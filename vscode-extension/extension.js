const vscode = require('vscode');
const http = require('http');

function activate(context) {
    console.log('PythonAI extension is now active!');

    let disposable = vscode.commands.registerCommand('pythonai.inlineChat', async () => {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            vscode.window.showInformationMessage('No active editor found.');
            return;
        }

        const selection = editor.selection;
        const selectedText = editor.document.getText(selection);
        
        if (!selectedText) {
            vscode.window.showWarningMessage('Please select some code to use Inline Chat.');
            return;
        }

        const prompt = await vscode.window.showInputBox({
            placeHolder: 'e.g. Optimize this function, add typing, or rewrite it...',
            prompt: 'PythonAI Inline Chat Request'
        });

        if (!prompt) {
            return; // User cancelled
        }

        const payload = JSON.stringify({
            file_path: editor.document.uri.fsPath,
            selected_text: selectedText,
            prompt: prompt,
            start_line: selection.start.line + 1,
            end_line: selection.end.line + 1
        });

        const options = {
            hostname: '127.0.0.1',
            port: 11435,
            path: '/api/inline_chat',
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Content-Length': Buffer.byteLength(payload)
            }
        };

        vscode.window.withProgress({
            location: vscode.ProgressLocation.Notification,
            title: "PythonAI is generating code...",
            cancellable: true
        }, (progress, token) => {
            return new Promise((resolve, reject) => {
                const req = http.request(options, (res) => {
                    let fullText = '';
                    
                    // We'll replace the selection with nothing first, then stream into it
                    editor.edit(editBuilder => {
                        editBuilder.replace(selection, '');
                    }).then(() => {
                        let currentPosition = selection.start;
                        
                        res.on('data', (chunk) => {
                            if (token.isCancellationRequested) {
                                res.destroy();
                                return;
                            }
                            
                            const textChunk = chunk.toString();
                            fullText += textChunk;
                            
                            // To stream text in VSCode, we insert it at the current position
                            editor.edit(editBuilder => {
                                editBuilder.insert(currentPosition, textChunk);
                            }).then(() => {
                                // Update position based on inserted text lines
                                const lines = textChunk.split('\n');
                                if (lines.length > 1) {
                                    currentPosition = currentPosition.translate(lines.length - 1, lines[lines.length - 1].length);
                                } else {
                                    currentPosition = currentPosition.translate(0, textChunk.length);
                                }
                            });
                        });

                        res.on('end', () => {
                            vscode.window.showInformationMessage('Inline Chat completed!');
                            resolve();
                        });
                    });
                });

                req.on('error', (e) => {
                    vscode.window.showErrorMessage(`Failed to connect to PythonAI server: ${e.message}. Is ide_server.py running?`);
                    reject(e);
                });

                req.write(payload);
                req.end();
                
                token.onCancellationRequested(() => {
                    req.destroy();
                    reject(new Error("Cancelled"));
                });
            });
        });
    });

    // Register Ghost Text Autocomplete Provider
    const provider = vscode.languages.registerInlineCompletionItemProvider({ pattern: '**' }, {
        async provideInlineCompletionItems(document, position, context, token) {
            // Get text before and after cursor
            const prefix = document.getText(new vscode.Range(new vscode.Position(0, 0), position));
            const suffix = document.getText(new vscode.Range(position, new vscode.Position(document.lineCount, 0)));

            const payload = JSON.stringify({
                prefix: prefix,
                suffix: suffix,
                file_path: document.uri.fsPath
            });

            const options = {
                hostname: '127.0.0.1',
                port: 11435,
                path: '/api/autocomplete',
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Content-Length': Buffer.byteLength(payload)
                }
            };

            return new Promise((resolve, reject) => {
                const req = http.request(options, (res) => {
                    let fullText = '';
                    res.on('data', chunk => fullText += chunk.toString());
                    res.on('end', () => {
                        if (token.isCancellationRequested) {
                            resolve([]);
                            return;
                        }
                        try {
                            const result = JSON.parse(fullText);
                            if (result.completion) {
                                resolve([new vscode.InlineCompletionItem(result.completion)]);
                            } else {
                                resolve([]);
                            }
                        } catch (e) {
                            resolve([]);
                        }
                    });
                });

                req.on('error', () => resolve([]));
                req.write(payload);
                req.end();
                
                token.onCancellationRequested(() => {
                    req.destroy();
                    resolve([]);
                });
            });
        }
    });

    context.subscriptions.push(disposable, provider);
}

function deactivate() {}

module.exports = {
    activate,
    deactivate
}
