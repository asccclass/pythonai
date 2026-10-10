const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('http');
const { endpoint, request } = require('../transport');
async function fixture(handler, body) {
    const server = http.createServer(handler);
    await new Promise(r => server.listen(0, '127.0.0.1', r));
    try { await body(new URL(`http://127.0.0.1:${server.address().port}/`)); }
    finally { await new Promise(r => server.close(r)); }
}
test('UTF-8 byte splits preserve Chinese and emoji; concurrent streams isolated', async () => {
    await fixture((_req, res) => {
        res.writeHead(200, { 'Content-Type': 'application/x-ndjson' });
        const value = Buffer.from(JSON.stringify({type:'start',task_id:'a'})+'\n'+
            JSON.stringify({type:'delta',text:'原始資料🙂\n'})+'\n'+JSON.stringify({type:'done'})+'\n');
        for (const byte of value) res.write(Buffer.from([byte])); res.end();
    }, async url => {
        const results = await Promise.all([request(url, {}, {}, null, true), request(url, {}, {}, null, true)]);
        assert.deepEqual(results, ['原始資料🙂\n','原始資料🙂\n']);
    });
});
for (const [name, content] of [['missing done','{"type":"start","task_id":"a"}\n{"type":"delta","text":"partial"}\n'],
    ['explicit error','{"type":"error"}\n'],['invalid UTF-8',Buffer.from([0xff])]]) {
    test(`reject ${name}`, async () => fixture((_req,res) => {
        res.writeHead(200, {'Content-Type':'application/x-ndjson'}); res.end(content);
    }, async url => assert.rejects(request(url,{}, {},null,true))));
}
test('cancel actively aborts request', async () => fixture((_req,res) => {
    res.writeHead(200,{'Content-Type':'application/x-ndjson'}); res.flushHeaders();
}, async url => {
    let cancel; const token = {onCancellationRequested(fn) { cancel=fn;return {dispose(){}}; }};
    const pending = request(url,{}, {},token,true); cancel(); await assert.rejects(pending,/Cancelled/);
}));
test('HTTP errors do not become replacement code', async () => fixture((_req,res) => {
    res.writeHead(401,{'Content-Type':'application/json'}); res.end('{}');
}, async url => assert.rejects(request(url,{}, {},null,true),/401/)));
test('redirects are rejected without forwarding credentials', async () => fixture((_req,res) => {
    res.writeHead(302,{'Location':'http://example.com','Content-Type':'application/x-ndjson'}); res.end();
}, async url => assert.rejects(request(url,{}, {},null,true),/302/)));
test('oversized wire response is bounded', async () => fixture((_req,res) => {
    res.writeHead(200,{'Content-Type':'application/x-ndjson'}); res.end(' '.repeat(2*1024*1024+1));
}, async url => assert.rejects(request(url,{}, {},null,true),/exceeds/)));
test('reject remote plaintext, embedded credentials and redirects', () => {
    assert.throws(() => endpoint('http://example.com','/')); assert.throws(() => endpoint('https://user:secret@example.com','/'));
    assert.equal(endpoint('http://127.0.0.1:11435','/api/inline_chat').pathname,'/api/inline_chat');
});
