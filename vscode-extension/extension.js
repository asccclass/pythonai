const vscode = require('vscode');
const { ReviewController } = require('./review');
const { endpoint, request } = require('./transport');
function activate(context) {
    const controller = new ReviewController(vscode, context);
    const register = (name, handler) => context.subscriptions.push(vscode.commands.registerCommand(name, async () => {
        try { return await handler(); } catch (error) { vscode.window.showErrorMessage(error.message); }
    }));
    context.subscriptions.push(vscode.workspace.registerTextDocumentContentProvider('pythonai-review', {
        provideTextDocumentContent: uri => controller.content(uri)
    }));
    register('pythonai.setToken', async () => {
        const token = await vscode.window.showInputBox({ password: true, prompt: 'PythonAI API Token (stored in SecretStorage)' });
        if (token) await context.secrets.store('pythonai.apiToken', token);
    });
    register('pythonai.inlineChat', async () => {
        const editor = vscode.window.activeTextEditor;
        if (!editor || !editor.document.getText(editor.selection)) throw new Error('Select code first');
        const originalVersion = editor.document.version;
        const originalSelection = editor.selection;
        const prompt = await vscode.window.showInputBox({ prompt: 'PythonAI replacement request' });
        if (!prompt) return;
        if (editor.document.version !== originalVersion || !editor.selection.isEqual(originalSelection)) throw new Error('Selection changed');
        return vscode.window.withProgress({ location: vscode.ProgressLocation.Notification,
            title: 'Generating a proposal; original source is retained', cancellable: true },
            (_progress, token) => controller.propose(editor, prompt, token));
    });
    for (const action of ['apply', 'undo', 'discard']) register(`pythonai.${action}Proposal`, () => controller.choose(action));
    register('pythonai.reviewTasks', () => vscode.env.openExternal(vscode.Uri.parse(
        endpoint(controller.config().get('dashboardUrl', 'http://127.0.0.1:8000'), '/').toString())));
    context.subscriptions.push(vscode.languages.registerInlineCompletionItemProvider({ scheme: 'file' }, {
        async provideInlineCompletionItems(document, position, _context, token) {
            const version = document.version;
            try {
                const headers = await controller.headers();
                const prefix = document.getText(new vscode.Range(new vscode.Position(0, 0), position)).slice(-2000);
                const result = await request(endpoint(controller.config().get('ideUrl', 'http://127.0.0.1:11435'), '/api/autocomplete'),
                    { prefix, file_path: document.uri.fsPath }, headers, token);
                return !token.isCancellationRequested && version === document.version && typeof result.completion === 'string'
                    ? [new vscode.InlineCompletionItem(result.completion)] : [];
            } catch { return []; }
        }
    }));
    return controller;
}
module.exports = { activate, deactivate() {} };
