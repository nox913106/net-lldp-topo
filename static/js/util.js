/**
 * Small helpers shared by every page.
 */

// Every request that changes something carries X-Requested-With: the server
// refuses one without it (app/core/security_middleware.py), which is what
// stops another site from submitting a form to this one with the operator's
// cookie. Done here, once, rather than in each of the pages' many fetch()
// calls - a call that forgot would fail with 403 instead of being unsafe.
//
// And when the session has ended (timed out, logged out elsewhere, revoked
// by an administrator) an API call answers 401: send the browser to the
// login page and back here afterwards, instead of every panel showing its
// own "failed to load".
(function () {
    const nativeFetch = window.fetch.bind(window);
    let redirecting = false;
    window.fetch = async function (input, init) {
        const url = typeof input === 'string' ? input : (input && input.url) || '';
        const sameOrigin = !/^[a-z]+:\/\//i.test(url) || url.startsWith(location.origin);
        const method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
        if (sameOrigin && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
            init = Object.assign({}, init);
            const headers = new Headers(init.headers || (input && input.headers) || {});
            if (!headers.has('X-Requested-With')) headers.set('X-Requested-With', 'mf-topomon');
            init.headers = headers;
        }
        const response = await nativeFetch(input, init);
        if (sameOrigin && !redirecting && location.pathname !== '/login'
                && !url.includes('/api/v1/auth/login')) {
            if (response.status === 401) {
                redirecting = true;
                location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
            } else if (response.status === 403) {
                const code = await response.clone().json().then(b => b.code).catch(() => null);
                if (code === 'password_change_required' || code === 'not_granted') {
                    redirecting = true;
                    location.href = '/login';
                }
            }
        }
        return response;
    };
})();

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
