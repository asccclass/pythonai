const crypto = require('crypto');
const { endpoint, request } = require('./transport');

class ReviewController {
    constructor(vscode, context) {
        this.vscode = vscode; this.context = context;
        this.proposals = new Map(); this.busy = new Set();
    }
    config() { return this.vscode.workspace.getConfiguration('pythonai'); }
    async headers() {
        const secret = await this.context.secrets.get('pythonai.apiToken');
        if (!secret) throw new Error('Run PythonAI: Set API Token first');
        return { Authorization: `Bearer ${secret}`, 'X-Agent-Project': this.config().get('project', 'default'),
            'X-Agent-Session': this.config().get('session', 'dashboard') };
    }
    async propose(editor, prompt, token) {
        const document = editor.document, uri = document.uri.toString();
        if (this.busy.has(uri)) throw new Error('A proposal is already running for this document');
        if (this.proposals.size >= 32) throw new Error('Discard old proposals before generating more');
        const version = document.version, selection = editor.selection;
        const selected = document.getText(selection), before = document.getText();
        if (!selected || Buffer.byteLength(before) > 1024 * 1024) throw new Error('Select code in a file below 1 MiB');
        this.busy.add(uri);
        try {
            const headers = await this.headers();
            let taskId;
            const afterSelection = await request(endpoint(this.config().get('ideUrl', 'http://127.0.0.1:11435'), '/api/inline_chat'),
                { file_path: document.uri.fsPath, selected_text: selected, prompt }, headers, token, true, id => { taskId = id; });
            if (token?.isCancellationRequested || document.isClosed || document.version !== version)
                throw new Error('Document changed or generation was cancelled; proposal discarded');
            if (afterSelection === selected) throw new Error('No change proposed');
            const start = document.offsetAt(selection.start), end = document.offsetAt(selection.end);
            const id = crypto.randomUUID();
            this.proposals.set(id, { id, taskId, document, version, selection, selected, before,
                afterSelection, after: before.slice(0, start) + afterSelection + before.slice(end), start, state: 'proposed' });
            await this.vscode.commands.executeCommand('vscode.diff',
                this.vscode.Uri.parse(`pythonai-review:/${id}/before`), this.vscode.Uri.parse(`pythonai-review:/${id}/after`),
                `PythonAI ${taskId} · proposal ${id}`, { preview: true });
            return id;
        } finally { this.busy.delete(uri); }
    }
    content(uri) {
        const [, id, side] = uri.path.split('/'), proposal = this.proposals.get(id);
        if (!proposal || !['before', 'after'].includes(side)) return 'Proposal discarded';
        return proposal[side];
    }
    async apply(id, undo = false) {
        const p = this.proposals.get(id);
        if (!p || p.state !== (undo ? 'applied' : 'proposed')) throw new Error('Proposal is not available for this action');
        const editor = await this.vscode.window.showTextDocument(p.document);
        if (p.state !== (undo ? 'applied' : 'proposed')) throw new Error('Proposal action is already in progress');
        if (p.document.isClosed || p.document.version !== (undo ? p.appliedVersion : p.version))
            throw new Error('Document changed; refusing stale apply/undo');
        const range = undo ? new this.vscode.Range(p.document.positionAt(p.start),
            p.document.positionAt(p.start + p.afterSelection.length)) : p.selection;
        p.state = 'applying';
        const accepted = await editor.edit(builder => builder.replace(range, undo ? p.selected : p.afterSelection),
            { undoStopBefore: true, undoStopAfter: true });
        if (!accepted) { p.state = undo ? 'applied' : 'proposed'; throw new Error('Editor rejected atomic edit'); }
        p.state = undo ? 'undone' : 'applied'; p.appliedVersion = p.document.version;
        return true;
    }
    async choose(action) {
        const items = [...this.proposals.values()].filter(p => action === 'discard' ||
            p.state === (action === 'undo' ? 'applied' : 'proposed')).map(p => ({ label: p.id,
                description: `${p.taskId} · ${p.document.uri.fsPath} (${p.state})` }));
        const item = await this.vscode.window.showQuickPick(items, { placeHolder: `PythonAI: ${action} proposal` });
        if (!item) return;
        if (action === 'discard') this.proposals.delete(item.label);
        else await this.apply(item.label, action === 'undo');
    }
}
module.exports = { ReviewController };
