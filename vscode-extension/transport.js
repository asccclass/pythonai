const http = require('http');
const https = require('https');
const { TextDecoder } = require('util');

function endpoint(value, route) {
    const url = new URL(value);
    if (url.username || url.password || url.search || url.hash ||
        (url.protocol !== 'https:' && !(url.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)))) {
        throw new Error('Use HTTPS or an HTTP loopback endpoint without embedded credentials');
    }
    url.pathname = route;
    return url;
}

function request(url, payload, headers, token, stream = false, onTask = () => {}) {
    return new Promise((resolve, reject) => {
        const decoder = new TextDecoder('utf-8', { fatal: true });
        let pending = '', output = '', size = 0, done = false, started = false, settled = false;
        let subscription, response;
        const finish = (error, value) => {
            if (settled) return;
            settled = true; clearTimeout(timer); subscription?.dispose();
            if (error) { response?.destroy(); req.destroy(); reject(error); } else resolve(value);
        };
        const parse = line => {
            const item = JSON.parse(line);
            if (done) throw new Error('Data after completion');
            if (item.type === 'start' && !started && typeof item.task_id === 'string') { started = true; onTask(item.task_id); }
            else if (item.type === 'delta' && started && typeof item.text === 'string') output += item.text;
            else if (item.type === 'done' && started) done = true;
            else throw new Error('Incomplete or failed generation');
        };
        const body = JSON.stringify(payload);
        const req = (url.protocol === 'https:' ? https : http).request(url, {
            method: 'POST', headers: { ...headers, 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) }
        }, res => {
            response = res;
            const expected = stream ? 'application/x-ndjson' : 'application/json';
            if (res.statusCode !== 200 || !(res.headers['content-type'] || '').startsWith(expected))
                return finish(new Error(`Unexpected response: HTTP ${res.statusCode}`));
            res.on('data', chunk => {
                try {
                    size += chunk.length;
                    if (size > 2 * 1024 * 1024) throw new Error('Response exceeds 2 MiB');
                    pending += decoder.decode(chunk, { stream: true });
                    if (stream) {
                        let newline;
                        while ((newline = pending.indexOf('\n')) >= 0) {
                            const line = pending.slice(0, newline); pending = pending.slice(newline + 1);
                            if (line) parse(line);
                        }
                    }
                } catch (error) { finish(error); }
            });
            res.on('aborted', () => finish(new Error('Connection interrupted')));
            res.on('error', error => finish(error));
            res.on('end', () => {
                try {
                    pending += decoder.decode();
                    if (stream && (pending.trim() || !done || !output)) throw new Error('Incomplete proposal');
                    finish(null, stream ? output : JSON.parse(pending));
                } catch (error) { finish(error); }
            });
        });
        const timer = setTimeout(() => finish(new Error('Request deadline exceeded')), 100000);
        req.on('error', error => finish(error));
        subscription = token?.onCancellationRequested(() => finish(new Error('Cancelled')));
        if (token?.isCancellationRequested) return finish(new Error('Cancelled'));
        req.end(body);
    });
}
module.exports = { endpoint, request };
