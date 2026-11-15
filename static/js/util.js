/**
 * Small helpers shared by every page.
 */

// Hostnames, port names and descriptions come from the devices over SNMP
// (sysName, ifDescr, LLDP), so they are somebody else's strings. Anything
// that goes into innerHTML goes through here first.
function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;',
        '"': '&quot;', "'": '&#39;'
    })[c]);
}

// fetch() only rejects when the network fails: a 404, a 405 and a 500 all
// resolve normally. Several pages closed their dialog on any answer at all,
// so a change the server had refused looked exactly like one it had saved.
// This turns a refusal into an Error carrying the server's own explanation.
async function apiRequest(url, options) {
    const response = await fetch(url, options);
    if (response.ok) {
        return response.status === 204 ? null : response.json();
    }
    let detail = `HTTP ${response.status}`;
    try {
        const body = await response.json();
        if (typeof body.detail === 'string') {
            detail = body.detail;
        } else if (Array.isArray(body.detail)) {
            // FastAPI's validation errors: [{loc, msg}, ...]
            detail = body.detail.map(e => e.msg || JSON.stringify(e)).join('; ');
        }
    } catch (e) { /* not JSON - keep the status */ }
    throw new Error(detail);
}

function apiJson(method, body) {
    return {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    };
}

window.escapeHtml = escapeHtml;
window.apiRequest = apiRequest;
window.apiJson = apiJson;
