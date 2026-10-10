const vscode = require('vscode');
const assert = require('assert/strict');
const http = require('http');
const fs = require('fs');
const path = require('path');
const os = require('os');

exports.run = async () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(),'pythonai-host-'));
    const server = http.createServer((req,res) => {
        assert.equal(req.headers.authorization,'Bearer '+ 'a'.repeat(32));
        let body='';req.on('data',chunk=>body+=chunk);req.on('end',()=> {
            const input=JSON.parse(body);
            res.writeHead(200,{'Content-Type':'application/x-ndjson'});
            const output=JSON.stringify({type:'start',task_id:'ide-fixture'})+'\n'+
                JSON.stringify({type:'delta',text:'新資料🙂\nnext'})+'\n';
            for (const byte of Buffer.from(output)) res.write(Buffer.from([byte]));
            if(input.prompt==='interrupt') return res.destroy();
            if(input.prompt==='missing') return res.end();
            setTimeout(()=>res.end('{"type":"done"}\n'),input.prompt==='slow'?250:10);
        });
    });
    await new Promise(r=>server.listen(0,'127.0.0.1',r));
    const extension=vscode.extensions.getExtension('undefined_publisher.pythonai-inline-chat');
    assert.ok(extension, 'Development extension is loaded');
    const controller=await extension.activate();
    await controller.context.secrets.store('pythonai.apiToken','a'.repeat(32));
    controller.config=()=>({get:(key,def)=>key==='ideUrl'?`http://127.0.0.1:${server.address().port}`:def});
    const make=async name=> {
        const file=path.join(directory,name+'.txt');fs.writeFileSync(file,'原始資料🙂\ntail');
        const doc=await vscode.workspace.openTextDocument(vscode.Uri.file(file));
        const editor=await vscode.window.showTextDocument(doc);
        editor.selection=new vscode.Selection(new vscode.Position(0,0),new vscode.Position(0,6));
        return editor;
    };
    let cases=0;
    try {
        const editor=await make('atomic'); const original=editor.document.getText();
        const id=await controller.propose(editor,'normal');
        assert.equal(editor.document.getText(),original);
        assert.equal(controller.proposals.get(id).taskId,'ide-fixture'); cases++;
        assert.ok(controller.content(vscode.Uri.parse(`pythonai-review:/${id}/after`)).includes('新資料🙂')); cases++;
        await controller.apply(id); assert.equal(editor.document.getText(),'新資料🙂\nnext\ntail'); cases++;
        await controller.apply(id,true);assert.equal(editor.document.getText(),original);cases++;
        const native=await make('native-undo'),nativeId=await controller.propose(native,'normal');
        await controller.apply(nativeId);await vscode.commands.executeCommand('undo');
        assert.equal(native.document.getText(),original);cases++;
        const commands=await vscode.commands.getCommands(true);
        for(const command of ['pythonai.inlineChat','pythonai.applyProposal','pythonai.undoProposal','pythonai.reviewTasks']) assert.ok(commands.includes(command));
        cases++;
        for(const prompt of ['interrupt','missing']) {
            const target=await make(prompt),before=target.document.getText();
            await assert.rejects(controller.propose(target,prompt));assert.equal(target.document.getText(),before);cases++;
        }
        const cancelEditor=await make('cancel'),source=new vscode.CancellationTokenSource();
        const pending=controller.propose(cancelEditor,'slow',source.token);setTimeout(()=>source.cancel(),40);
        await assert.rejects(pending);assert.equal(cancelEditor.document.getText(),original);source.dispose();cases++;
        const overlap=await make('overlap');const first=controller.propose(overlap,'slow');
        await assert.rejects(controller.propose(overlap,'normal'),/already running/);await first;cases++;
        const stale=await make('stale'),staleId=await controller.propose(stale,'normal');
        const focus=await vscode.window.showTextDocument(stale.document);
        await focus.edit(edit=>edit.insert(new vscode.Position(0,0),'user'));
        await assert.rejects(controller.apply(staleId),/Document changed/);cases++;
        const changing=await make('changed-during-stream'),run=controller.propose(changing,'slow');
        await new Promise(r=>setTimeout(r,60));await changing.edit(edit=>edit.insert(new vscode.Position(0,0),'edit'));
        await assert.rejects(run,/Document changed/);cases++;
        const undoTarget=await make('stale-undo'),undoId=await controller.propose(undoTarget,'normal');
        await controller.apply(undoId);const undoEditor=await vscode.window.showTextDocument(undoTarget.document);
        await undoEditor.edit(edit=>edit.insert(new vscode.Position(0,0),'user'));
        await assert.rejects(controller.apply(undoId,true),/Document changed/);cases++;
        const left=await make('parallel-left'),right=await make('parallel-right');
        const pair=await Promise.all([controller.propose(left,'slow'),controller.propose(right,'normal')]);
        assert.notEqual(pair[0],pair[1]);assert.equal(left.document.getText(),original);assert.equal(right.document.getText(),original);cases++;
        fs.writeFileSync(path.join(__dirname,'../.vscode-test/result.json'),JSON.stringify({passed:cases,host:vscode.version}));
        console.log(`PythonAI real Extension Host: ${cases} cases passed (VS Code ${vscode.version})`);
    } finally {
        await controller.context.secrets.delete('pythonai.apiToken');
        await vscode.workspace.saveAll(false);
        await vscode.commands.executeCommand('workbench.action.closeAllEditors');
        await new Promise(r=>server.close(r));
        assert.equal(path.dirname(path.resolve(directory)),path.resolve(os.tmpdir()));
        assert.ok(path.basename(directory).startsWith('pythonai-host-'));
        fs.rmSync(directory,{recursive:true,force:true});
    }
};
