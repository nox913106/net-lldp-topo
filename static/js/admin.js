/**
 * The back office (static/admin.html). One page, one tab at a time, each tab
 * rendered from its API. Admin only - the server enforces that for every
 * call (app/core/security_middleware.py); this file only draws.
 */
(function () {
    'use strict';

    const T = (key, vars) => {
        let text = i18n.t(key);
        if (vars) for (const [k, v] of Object.entries(vars)) text = text.split(`{${k}}`).join(v);
        return text;
    };
    const esc = escapeHtml;
    const main = document.getElementById('admin-main');
    const API = '/api/v1/admin';

    // -- helpers -------------------------------------------------------------

    async function api(path, method = 'GET', body) {
        const opts = { method };
        if (body !== undefined) {
            opts.headers = { 'Content-Type': 'application/json' };
            opts.body = JSON.stringify(body);
        }
        return apiRequest(path.startsWith('/') ? path : `${API}/${path}`, opts);
    }

    function fmtTime(iso) {
        if (!iso) return '';
        const d = new Date(iso);
        if (isNaN(d)) return esc(iso);
        const p = n => String(n).padStart(2, '0');
        return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    }

    function fmtBytes(n) {
        if (n === null || n === undefined) return '';
        const u = ['B', 'KB', 'MB', 'GB', 'TB'];
        let i = 0;
        while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
        return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
    }

    function fmtDuration(seconds) {
        if (seconds === null || seconds === undefined) return '';
        const d = Math.floor(seconds / 86400), h = Math.floor(seconds % 86400 / 3600), m = Math.floor(seconds % 3600 / 60);
        return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
    }

    let toastTimer = null;
    function toast(message, isError = false) {
        const el = document.getElementById('admin-toast');
        el.textContent = message;
        el.classList.toggle('error', isError);
        el.hidden = false;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 8000 : 3500);
    }

    async function run(button, fn, okMessage) {
        if (button) button.disabled = true;
        try {
            const result = await fn();
            if (okMessage) toast(okMessage);
            return result;
        } catch (err) {
            toast(err.message || String(err), true);
            return undefined;
        } finally {
            if (button) button.disabled = false;
        }
    }

    // Modal: returns a promise of the form values, or null when cancelled
    function modal({ title, html, okLabel, danger, onOpen, wide }) {
        const overlay = document.getElementById('admin-modal');
        const body = document.getElementById('admin-modal-body');
        const footer = document.getElementById('admin-modal-footer');
        document.getElementById('admin-modal-title').textContent = title;
        body.innerHTML = html;
        overlay.querySelector('.admin-modal').style.width = wide ? 'min(960px, 100%)' : '';
        footer.innerHTML = okLabel
            ? `<button type="button" class="btn-secondary" data-act="cancel">${esc(T('cancel'))}</button>
               <button type="button" class="${danger ? 'btn-danger' : 'btn-save'}" data-act="ok">${esc(okLabel)}</button>`
            : `<button type="button" class="btn-secondary" data-act="cancel">${esc(T('close'))}</button>`;
        overlay.hidden = false;
        return new Promise(resolve => {
            const close = value => {
                overlay.hidden = true;
                body.innerHTML = '';
                overlay.removeEventListener('click', onClick);
                document.removeEventListener('keydown', onKey);
                resolve(value);
            };
            const collect = () => {
                const values = {};
                body.querySelectorAll('[name]').forEach(el => {
                    values[el.name] = el.type === 'checkbox' ? el.checked : el.value;
                });
                return values;
            };
            const onClick = e => {
                if (e.target === overlay || e.target.id === 'admin-modal-x' || e.target.dataset.act === 'cancel') close(null);
                else if (e.target.dataset.act === 'ok') close(collect());
            };
            const onKey = e => {
                if (e.key === 'Escape') close(null);
                if (e.key === 'Enter' && okLabel && e.target.tagName === 'INPUT') close(collect());
            };
            overlay.addEventListener('click', onClick);
            document.addEventListener('keydown', onKey);
            const first = body.querySelector('input');
            if (first) first.focus();
            if (onOpen) onOpen(body, close);
        });
    }

    // Watch a runner job until it ends, showing its output
    function watchJob(jobId, kind, onDone) {
        let stopped = false;
        let failures = 0;
        modal({
            title: T('kind_' + kind),
            wide: true,
            html: `<div id="job-state"><span class="badge info">${esc(T('jobQueued'))}</span> <span class="admin-hint">${esc(T('jobWaiting'))}</span></div>
                   <pre class="admin-pre" id="job-log"></pre>`,
            onOpen: async () => {
                while (!stopped) {
                    try {
                        const j = await api(`jobs/${jobId}`);
                        failures = 0;
                        const box = document.getElementById('job-state');
                        const log = document.getElementById('job-log');
                        if (!box) return;
                        box.innerHTML = `${jobBadge(j.state)} <span class="admin-hint">${esc(j.summary || (j.state === 'queued' ? T('jobWaiting') : ''))}</span>`;
                        if (j.log !== undefined) {
                            const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
                            log.textContent = j.log;
                            if (atBottom) log.scrollTop = log.scrollHeight;
                        }
                        if (j.state === 'ok' || j.state === 'failed') {
                            if (onDone) onDone(j);
                            return;
                        }
                    } catch (err) {
                        failures++;
                        const box = document.getElementById('job-state');
                        if (box) box.innerHTML = `<span class="badge warn">${esc(T('jobRunning'))}</span> <span class="admin-hint">${esc(T('jobReconnect'))}</span>`;
                    }
                    await new Promise(r => setTimeout(r, failures ? 3000 : 1500));
                }
            }
        }).then(() => { stopped = true; });
    }

    function jobBadge(state) {
        const cls = { ok: 'ok', failed: 'fail', running: 'warn', queued: 'info' }[state] || 'info';
        const key = { ok: 'jobOk', failed: 'jobFailed', running: 'jobRunning', queued: 'jobQueued' }[state];
        return `<span class="badge ${cls}">${esc(key ? T(key) : state || '')}</span>`;
    }

    function jobsTable(items) {
        if (!items || !items.length) return `<div class="admin-empty">${esc(T('none'))}</div>`;
        return `<div class="admin-table-wrap"><table class="admin-table">
            <tr><th>${esc(T('time'))}</th><th>${esc(T('jobKind'))}</th><th>${esc(T('jobBy'))}</th><th>${esc(T('status'))}</th><th>${esc(T('detail'))}</th></tr>
            ${items.map(j => `<tr>
                <td class="nw mono">${fmtTime(j.finished_at || j.started_at || j.requested_at)}</td>
                <td class="nw">${esc(T('kind_' + j.kind))}</td>
                <td class="mono">${esc(j.by || '')}</td>
                <td>${jobBadge(j.state)}</td>
                <td>${esc(j.summary || '')} <button type="button" class="btn-link" data-job="${esc(j.id)}" data-kind="${esc(j.kind)}">${esc(T('jobLog'))}</button></td>
            </tr>`).join('')}</table></div>`;
    }

    function bindJobLinks(root) {
        root.querySelectorAll('[data-job]').forEach(b => b.addEventListener('click', () => watchJob(b.dataset.job, b.dataset.kind)));
    }

    function section(title, body, actions = '') {
        return `<section class="settings-section"><div class="section-header"><h2>${esc(title)}</h2>
            <div class="admin-actions">${actions}</div></div><div class="section-content">${body}</div></section>`;
    }

    function runnerBanner(runner) {
        if (runner && runner.available) return '';
        return `<div class="admin-banner warn">${esc(T('runnerMissing'))}</div>`;
    }

    function field(label, inner, cls = '') {
        return `<label class="admin-field ${cls}"><span>${esc(label)}</span>${inner}</label>`;
    }

    function input(name, value, attrs = '') {
        return `<input type="text" name="${name}" value="${esc(value ?? '')}" ${attrs}>`;
    }

    function num(name, value, min, max) {
        return `<input type="number" name="${name}" value="${esc(value ?? '')}" min="${min}" max="${max}">`;
    }

    function check(name, checked, label) {
        return `<label class="admin-check"><input type="checkbox" name="${name}" ${checked ? 'checked' : ''}> ${esc(label)}</label>`;
    }

    function select(name, value, options) {
        return `<select name="${name}">${options.map(([v, l]) =>
            `<option value="${esc(v)}" ${String(v) === String(value) ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>`;
    }

    function formValues(form) {
        const values = {};
        form.querySelectorAll('[name]').forEach(el => {
            if (el.type === 'checkbox') values[el.name] = el.checked;
            else if (el.type === 'number') values[el.name] = el.value === '' ? null : Number(el.value);
            else values[el.name] = el.value;
        });
        return values;
    }

    const ROLES = [['admin', 'roleAdmin'], ['operator', 'roleOperator'], ['viewer', 'roleViewer'], ['guest', 'roleGuest']];
    const roleOptions = (roles = ROLES) => roles.map(([v, k]) => [v, T(k)]);

    // -- tabs ------------------------------------------------------------------

    const tabs = {};

    // System status
    tabs.status = async () => {
        const data = await api('health');
        const jobs = await api('jobs?limit=15');
        const last = data.last;
        const s = data.settings;
        const levelBadge = lvl => `<span class="badge ${lvl}">${esc(T({ ok: 'levelOk', info: 'levelInfo', warn: 'levelWarn', fail: 'levelFail' }[lvl]))}</span>`;
        let body = '';
        if (!last) {
            body = `<p class="admin-hint">${esc(T('noHealthYet'))}</p>`;
        } else {
            const abnormal = last.items.filter(i => i.level === 'warn' || i.level === 'fail');
            const row = i => `<tr><td class="nw">${levelBadge(i.level)}</td><td class="nw">${esc(i.name)}</td>
                <td>${esc(i.detail)}${i.hint ? `<div class="admin-hint"><code>${esc(i.hint)}</code></div>` : ''}</td></tr>`;
            body += `<p class="admin-hint">${esc(T('lastChecked'))}：${fmtTime(last.at)}（${esc(T(last.trigger === 'manual' ? 'manual' : 'scheduled'))}）
                ${['ok', 'info', 'warn', 'fail'].filter(k => last.counts[k]).map(k => `${levelBadge(k)} ${last.counts[k]}`).join(' ')}</p>`;
            body += abnormal.length
                ? `<div class="admin-banner ${last.counts.fail ? 'fail' : 'warn'}" style="margin-top:.8rem"><strong>${esc(T('needsAttention'))}（${abnormal.length}）</strong>
                    <table class="admin-table" style="margin-top:.4rem">${abnormal.map(row).join('')}</table></div>`
                : `<div class="admin-banner ok" style="margin-top:.8rem">${esc(T('allOk'))}</div>`;
            const groups = {};
            last.items.forEach(i => { (groups[i.group] = groups[i.group] || []).push(i); });
            body += `<h3 class="admin-hint" style="margin:1rem 0 .3rem">${esc(T('allItems'))}</h3><div class="admin-table-wrap"><table class="admin-table">
                ${Object.entries(groups).map(([g, items]) => `<tr class="group-row"><th colspan="3">${esc(g)}</th></tr>${items.map(row).join('')}`).join('')}</table></div>`;
        }
        const settingsForm = `<form id="health-form" class="admin-form">
            ${check('enabled', s.enabled, T('healthSettings'))}
            ${field(T('healthEnabled'), num('interval_hours', s.interval_hours, 1, 168))}
            ${field(T('certWarnDays'), num('cert_warn_days', s.cert_warn_days, 1, 365))}
            ${field(T('diskWarn'), num('disk_warn_percent', s.disk_warn_percent, 50, 99))}
            ${field(T('backupMaxAge'), num('backup_max_age_hours', s.backup_max_age_hours, 1, 2160))}
            ${check('notify_ok', s.notify_ok, T('healthNotifyOk'))}
            <p class="admin-hint wide">${esc(T('healthHint'))}</p>
            <div class="admin-form-actions wide"><button type="submit" class="btn-save">${esc(T('save'))}</button></div></form>`;
        const doctor = data.doctor;
        main.innerHTML =
            section(T('healthCheck'), body,
                `<button type="button" class="btn-save" id="health-run">${esc(T('runNow'))}</button>`) +
            section(T('doctorTitle'),
                `${runnerBanner(data.runner)}<p class="admin-hint">${esc(T('doctorHint'))}</p>
                 ${doctor ? `<p class="admin-hint" style="margin-top:.6rem">${fmtTime(doctor.finished_at || doctor.requested_at)} ${jobBadge(doctor.state)}
                    <button type="button" class="btn-link" data-job="${esc(doctor.id)}" data-kind="doctor">${esc(T('jobLog'))}</button></p>` : ''}`,
                `<button type="button" class="btn-secondary" id="doctor-run" ${data.runner.available ? '' : 'disabled'}>${esc(T('runDoctor'))}</button>`) +
            section(T('healthSettings'), settingsForm) +
            section(T('jobs'), jobsTable(jobs.items));
        bindJobLinks(main);
        document.getElementById('health-run').addEventListener('click', e =>
            run(e.target, async () => { await api('health/run', 'POST'); await tabs.status(); }));
        document.getElementById('doctor-run').addEventListener('click', e =>
            run(e.target, async () => {
                const r = await api('doctor', 'POST');
                watchJob(r.job, 'doctor', () => tabs.status());
            }));
        document.getElementById('health-form').addEventListener('submit', e => {
            e.preventDefault();
            run(e.submitter, () => api('settings/health', 'PUT', formValues(e.target)), T('saved'));
        });
    };

    // Accounts
    tabs.users = async () => {
        const users = await api('users');
        const auth = (await api('settings/auth')).value;
        const me = window.currentUser || {};
        const rows = users.map(u => `<tr data-id="${u.id}">
            <td class="mono">${esc(u.username)}${u.id === me.id ? ' <span class="badge accent">me</span>' : ''}</td>
            <td>${esc(u.display_name)}${u.email ? `<div class="admin-hint">${esc(u.email)}</div>` : ''}</td>
            <td><span class="badge">${esc(T(u.source === 'ldap' ? 'sourceLdap' : 'sourceLocal'))}</span></td>
            <td>${select('role', u.role, roleOptions())}</td>
            <td><input type="checkbox" name="is_active" ${u.is_active ? 'checked' : ''} aria-label="${esc(T('active'))}"></td>
            <td class="nw mono">${u.last_login_at ? fmtTime(u.last_login_at) : esc(T('never'))}
                ${u.locked_until && new Date(u.locked_until) > new Date() ? ` <span class="badge fail">${esc(T('locked'))}</span>` : ''}
                ${u.must_change_password ? ` <span class="badge warn">${esc(T('mustChange'))}</span>` : ''}</td>
            <td class="num">${u.sessions}</td>
            <td class="nw">
                ${u.source === 'local' ? `<button type="button" class="btn-link" data-act="password">${esc(T('resetPassword'))}</button> · ` : ''}
                <button type="button" class="btn-link" data-act="logout">${esc(T('logoutEverywhere'))}</button> ·
                <button type="button" class="btn-link danger" data-act="delete">${esc(T('deleteAccount'))}</button>
            </td></tr>`).join('');
        main.innerHTML =
            section(T('accounts'),
                `<p class="admin-hint" style="margin-bottom:.8rem">${esc(T('rolesHint'))}</p>
                 <div class="admin-table-wrap"><table class="admin-table" id="users-table">
                 <tr><th>${esc(T('username'))}</th><th>${esc(T('displayName'))}</th><th>${esc(T('source'))}</th><th>${esc(T('role'))}</th>
                 <th>${esc(T('active'))}</th><th>${esc(T('lastLogin'))}</th><th>${esc(T('sessions'))}</th><th>${esc(T('actions'))}</th></tr>
                 ${rows}</table></div>`,
                `<button type="button" class="btn-save" id="user-add">${esc(T('addAccount'))}</button>`) +
            section(T('loginSettings'), `<form id="auth-form" class="admin-form">
                ${field(T('idleMinutes'), num('idle_minutes', auth.idle_minutes, 5, 10080))}
                ${field(T('absoluteHours'), num('absolute_hours', auth.absolute_hours, 1, 2160))}
                ${field(T('lockoutFailures'), num('lockout_failures', auth.lockout_failures, 3, 50))}
                ${field(T('lockoutMinutes'), num('lockout_minutes', auth.lockout_minutes, 1, 1440))}
                <div class="admin-form-actions wide"><button type="submit" class="btn-save">${esc(T('save'))}</button></div></form>`);

        const table = document.getElementById('users-table');
        table.querySelectorAll('tr[data-id]').forEach(tr => {
            const id = Number(tr.dataset.id);
            const user = users.find(u => u.id === id);
            tr.querySelector('[name="role"]').addEventListener('change', e =>
                run(null, () => api(`users/${id}`, 'PUT', { role: e.target.value }), T('saved')).then(r => { if (r === undefined) tabs.users(); }));
            tr.querySelector('[name="is_active"]').addEventListener('change', e =>
                run(null, () => api(`users/${id}`, 'PUT', { is_active: e.target.checked }), T('saved')).then(r => { if (r === undefined) tabs.users(); }));
            tr.querySelectorAll('[data-act]').forEach(b => b.addEventListener('click', async () => {
                if (b.dataset.act === 'password') {
                    const v = await modal({
                        title: T('newPasswordFor', { name: user.username }), okLabel: T('resetPassword'),
                        html: `${field(T('newPassword'), '<input type="password" name="password" autocomplete="new-password">')}
                               <p class="admin-hint">${esc(T('passwordRules'))}</p>${check('must_change_password', true, T('requireChange'))}`
                    });
                    if (v) run(null, () => api(`users/${id}/password`, 'POST', v), T('passwordChanged')).then(() => tabs.users());
                } else if (b.dataset.act === 'logout') {
                    run(b, () => api(`users/${id}/logout`, 'POST'), T('saved')).then(() => tabs.users());
                } else if (b.dataset.act === 'delete') {
                    const v = await modal({ title: T('confirmDelete', { name: user.username }), okLabel: T('deleteAccount'), danger: true, html: '' });
                    if (v) run(null, () => api(`users/${id}`, 'DELETE'), T('saved')).then(() => tabs.users());
                }
            }));
        });
        document.getElementById('user-add').addEventListener('click', async () => {
            const v = await modal({
                title: T('addAccount'), okLabel: T('addAccount'),
                html: `<div class="admin-form">
                    ${field(T('username'), input('username', '', 'autocomplete="off" spellcheck="false"'))}
                    ${field(T('displayName'), input('display_name', ''))}
                    ${field(T('email'), input('email', ''))}
                    ${field(T('role'), select('role', 'viewer', roleOptions(ROLES.slice(0, 3))))}
                    ${field(T('source'), select('source', 'local', [['local', T('sourceLocal')], ['ldap', T('sourceLdap')]]))}
                    ${field(T('password'), '<input type="password" name="password" autocomplete="new-password">')}
                    </div><p class="admin-hint">${esc(T('passwordRules'))} ${esc(T('ldapPreRegister'))}</p>
                    ${check('must_change_password', true, T('requireChange'))}`
            });
            if (v) run(null, () => api('users', 'POST', v), T('saved')).then(() => tabs.users());
        });
        document.getElementById('auth-form').addEventListener('submit', e => {
            e.preventDefault();
            run(e.submitter, () => api('settings/auth', 'PUT', formValues(e.target)), T('saved'));
        });
    };

    // Directory login
    tabs.ldap = async () => {
        const c = (await api('settings/ldap')).value;
        main.innerHTML = section(T('tabLdap'), `<p class="admin-hint" style="margin-bottom:.9rem">${esc(T('ldapExplain'))}</p>
            <form id="ldap-form" class="admin-form">
                <div class="wide">${check('enabled', c.enabled, T('ldapEnabled'))}</div>
                ${field(T('ldapUrls'), `<textarea name="urls" rows="2" spellcheck="false" placeholder="ldaps://dc01.corp.local:636">${esc((c.urls || []).join('\n'))}</textarea>`, 'wide')}
                ${field(T('ldapDomain'), input('domain', c.domain, 'placeholder="corp.local" spellcheck="false"'))}
                ${field(T('ldapBaseDn'), input('base_dn', c.base_dn, 'placeholder="DC=corp,DC=local" spellcheck="false"'))}
                ${field(T('ldapGroups'), `<textarea name="groups" rows="3" spellcheck="false" placeholder="CN=NetOps,OU=Groups,DC=corp,DC=local">${esc((c.groups || []).join('\n'))}</textarea>`, 'wide')}
                ${field(T('ldapDefaultRole'), select('default_role', c.default_role, roleOptions([ROLES[3], ROLES[2], ROLES[1]])))}
                ${field(T('ldapTimeout'), num('timeout', c.timeout, 1, 60))}
                ${field(T('ldapHint'), input('login_hint', c.login_hint, 'placeholder="corp.local"'))}
                ${field(T('ldapCa'), `<textarea name="ca_pem" rows="4" spellcheck="false" placeholder="-----BEGIN CERTIFICATE-----">${esc(c.ca_pem || '')}</textarea>`, 'wide')}
                <div class="wide" style="border-top:1px solid var(--t-grid);padding-top:.9rem"><p class="admin-hint">${esc(T('ldapTestHint'))}</p></div>
                ${field(T('ldapTestUser'), '<input type="text" id="ldap-test-user" autocomplete="off" spellcheck="false">')}
                ${field(T('ldapTestPassword'), '<input type="password" id="ldap-test-password" autocomplete="new-password">')}
                <div class="wide" id="ldap-result"></div>
                <div class="admin-form-actions wide">
                    <button type="button" class="btn-secondary" id="ldap-test">${esc(T('ldapTest'))}</button>
                    <button type="submit" class="btn-save">${esc(T('save'))}</button></div>
            </form>`);
        const form = document.getElementById('ldap-form');
        const values = () => {
            const v = formValues(form);
            return { ...v, urls: v.urls.split('\n'), groups: v.groups.split('\n') };
        };
        document.getElementById('ldap-test').addEventListener('click', e => run(e.target, async () => {
            const r = await api('ldap/test', 'POST', {
                settings: values(),
                username: document.getElementById('ldap-test-user').value,
                password: document.getElementById('ldap-test-password').value
            });
            document.getElementById('ldap-result').innerHTML = `<table class="admin-table">${r.steps.map(s => `<tr>
                <td class="nw"><span class="badge ${s.ok === true ? 'ok' : s.ok === false ? 'fail' : 'info'}">${esc(T(s.ok === true ? 'testPassed' : s.ok === false ? 'testFailed' : 'testNote'))}</span></td>
                <td class="nw">${esc(s.step)}</td><td>${esc(s.detail)}</td></tr>`).join('')}</table>`;
        }));
        form.addEventListener('submit', e => {
            e.preventDefault();
            run(e.submitter, () => api('settings/ldap', 'PUT', values()), T('saved'));
        });
    };

    // Audit log
    const auditState = { offset: 0, items: [] };
    tabs.audit = async (keep) => {
        const today = new Date();
        const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
        const daysAgo = n => { const d = new Date(today); d.setDate(d.getDate() - n); return iso(d); };
        const f = keep ? auditState.filter : { date_from: daysAgo(6), date_to: iso(today), action: '', username: '', q: '' };
        auditState.filter = f;
        const params = new URLSearchParams({ ...f, tz_offset: String(-today.getTimezoneOffset()), limit: '200', offset: '0' });
        const data = await api(`audit?${params}`);
        auditState.items = data.items;
        auditState.total = data.total;
        auditState.params = params;
        const rows = items => items.map(r => `<tr>
            <td class="nw mono">${fmtTime(r.at)}</td><td class="mono">${esc(r.username || '')}</td>
            <td class="nw"><code>${esc(r.action)}</code></td><td>${esc(r.target || '')}</td>
            <td class="num">${r.status ?? ''}</td><td class="mono">${esc(r.ip || '')}</td>
            <td class="audit-detail">${r.detail ? esc(JSON.stringify(r.detail)) : ''}</td></tr>`).join('');
        main.innerHTML = section(T('tabAudit'), `<p class="admin-hint" style="margin-bottom:.9rem">${esc(T('auditExplain'))}</p>
            <form id="audit-form" class="admin-form">
                ${field(T('dateFrom'), `<input type="date" name="date_from" value="${esc(f.date_from)}">`)}
                ${field(T('dateTo'), `<input type="date" name="date_to" value="${esc(f.date_to)}">`)}
                ${field(T('action'), select('action', f.action, [['', T('all')], ...data.actions.map(a => [a, a])]))}
                ${field(T('user'), select('username', f.username, [['', T('all')], ...data.usernames.map(u => [u, u])]))}
                ${field(T('keyword'), input('q', f.q))}
                <div class="admin-form-actions wide" style="justify-content:space-between">
                    <span class="admin-actions">
                        <button type="button" class="btn-link" data-range="0">${esc(T('today'))}</button>
                        <button type="button" class="btn-link" data-range="6">${esc(T('last7'))}</button>
                        <button type="button" class="btn-link" data-range="29">${esc(T('last30'))}</button></span>
                    <span class="admin-actions">
                        <a class="btn-secondary" id="audit-csv" href="${API}/audit/export?${params}">${esc(T('exportCsv'))}</a>
                        <button type="submit" class="btn-save">${esc(T('search'))}</button></span></div>
            </form>
            <p class="admin-hint" style="margin:.8rem 0 .4rem">${esc(T('auditTotal', { n: data.total }))}</p>
            <div class="admin-table-wrap"><table class="admin-table" id="audit-table">
                <tr><th>${esc(T('time'))}</th><th>${esc(T('user'))}</th><th>${esc(T('action'))}</th><th>${esc(T('target'))}</th>
                <th>HTTP</th><th>${esc(T('address'))}</th><th>${esc(T('detail'))}</th></tr>${rows(data.items)}</table></div>
            ${data.total > data.items.length ? `<div class="admin-form-actions"><button type="button" class="btn-secondary" id="audit-more">${esc(T('moreRows'))}</button></div>` : ''}`) +
            section(T('auditRetention'), `<form id="retention-form" class="admin-form">
                ${field(T('auditRetention'), num('retention_days', data.retention_days, 30, 3650))}
                <p class="admin-hint wide">${esc(T('auditRetentionHint', { date: data.earliest ? fmtTime(data.earliest) : T('none') }))}</p>
                <div class="admin-form-actions wide"><button type="submit" class="btn-save">${esc(T('save'))}</button></div></form>`);
        const form = document.getElementById('audit-form');
        form.addEventListener('submit', e => {
            e.preventDefault();
            auditState.filter = formValues(form);
            run(e.submitter, () => tabs.audit(true));
        });
        form.querySelectorAll('[data-range]').forEach(b => b.addEventListener('click', () => {
            auditState.filter = { ...formValues(form), date_from: daysAgo(Number(b.dataset.range)), date_to: iso(today) };
            run(null, () => tabs.audit(true));
        }));
        const more = document.getElementById('audit-more');
        if (more) more.addEventListener('click', () => run(more, async () => {
            const p = new URLSearchParams(auditState.params);
            p.set('offset', String(auditState.items.length));
            const next = await api(`audit?${p}`);
            auditState.items = auditState.items.concat(next.items);
            document.getElementById('audit-table').insertAdjacentHTML('beforeend', rows(next.items));
            if (auditState.items.length >= auditState.total) more.remove();
        }));
        document.getElementById('retention-form').addEventListener('submit', e => {
            e.preventDefault();
            run(e.submitter, () => api('settings/audit', 'PUT', formValues(e.target)), T('saved'));
        });
    };

    // Notifications
    let notifyDraft = null;
    tabs.notify = async () => {
        const data = await api('notifications');
        const deliveries = await api('notifications/deliveries?limit=50');
        notifyDraft = data.value;
        const defaults = data.defaults;
        const EVENTS = ['alert_new', 'alert_resolved', 'health_failed', 'job_finished'];
        const channelCard = (ch, i) => {
            const common = `
                ${field(T('channelName'), input(`ch.${i}.name`, ch.name))}
                ${check(`ch.${i}.enabled`, ch.enabled, T('enabled'))}`;
            let specific = '';
            if (ch.type === 'teams') {
                specific = `${field(T('webhookUrl'), input(`ch.${i}.url`, ch.url, 'spellcheck="false" placeholder="https://…"'), 'wide')}
                    <p class="admin-hint wide">${esc(T('teamsHint'))}</p>`;
            } else if (ch.type === 'webhook') {
                specific = `${field(T('webhookUrl'), input(`ch.${i}.url`, ch.url, 'spellcheck="false" placeholder="https://…"'), 'wide')}
                    ${field(T('webhookSecret'), `<input type="password" name="ch.${i}.secret" value="${esc(ch.secret)}" autocomplete="new-password">`, 'wide')}`;
            } else {
                specific = `${field(T('smtpHost'), input(`ch.${i}.smtp_host`, ch.smtp_host, 'spellcheck="false"'))}
                    ${field(T('smtpPort'), num(`ch.${i}.smtp_port`, ch.smtp_port, 1, 65535))}
                    ${field(T('smtpSecurity'), select(`ch.${i}.smtp_security`, ch.smtp_security, [['starttls', 'STARTTLS'], ['ssl', 'SSL/TLS'], ['none', 'None']]))}
                    ${field(T('smtpUser'), input(`ch.${i}.smtp_user`, ch.smtp_user, 'autocomplete="off"'))}
                    ${field(T('smtpPassword'), `<input type="password" name="ch.${i}.smtp_password" value="${esc(ch.smtp_password)}" autocomplete="new-password">`)}
                    ${field(T('mailFrom'), input(`ch.${i}.mail_from`, ch.mail_from))}
                    ${field(T('mailTo'), input(`ch.${i}.mail_to`, (ch.mail_to || []).join(', ')), 'wide')}`;
            }
            return `<div class="channel-card" data-index="${i}">
                <header><span class="badge accent">${esc(ch.type)}</span>
                    <span class="admin-actions">
                        <button type="button" class="btn-secondary" data-test="${i}">${esc(T('sendTest'))}</button>
                        <button type="button" class="btn-link danger" data-remove="${i}">${esc(T('removeChannel'))}</button></span></header>
                <div class="admin-form">${common}${specific}
                    <div class="admin-field wide"><span>${esc(T('events'))}</span><div class="channel-events">
                        ${EVENTS.map(ev => check(`ch.${i}.ev.${ev}`, (ch.events || []).includes(ev), T('ev_' + ev))).join('')}</div></div>
                    ${field(T('minSeverity'), select(`ch.${i}.min_severity`, ch.min_severity, [['info', T('sevInfo')], ['warning', T('sevWarning')], ['critical', T('sevCritical')]]))}
                </div></div>`;
        };
        const draw = () => {
            const v = notifyDraft;
            const templates = EVENTS.map(ev => {
                const t = v.templates[ev] || defaults[ev];
                return `<div class="tpl-block"><div class="tpl-head"><strong>${esc(T('ev_' + ev))}</strong>
                    <button type="button" class="btn-link" data-reset="${ev}">${esc(T('resetTemplate'))}</button></div>
                    <div class="admin-form">${field(T('templateTitle'), input(`tpl.${ev}.title`, t.title), 'wide')}
                    ${field(T('templateBody'), `<textarea name="tpl.${ev}.body" rows="3">${esc(t.body)}</textarea>`, 'wide')}</div></div>`;
            }).join('');
            const statusBadge = st => `<span class="badge ${{ sent: 'ok', pending: 'warn', failed: 'fail' }[st] || 'info'}">${esc(T('st_' + st))}</span>`;
            main.innerHTML = `<form id="notify-form">` +
                section(T('tabNotify'), `<div class="admin-form">
                    <div class="wide">${check('enabled', v.enabled, T('notifyEnabled'))}</div>
                    ${field(T('publicUrl'), input('public_url', v.public_url, 'placeholder="https://topomon.corp.local" spellcheck="false"'), 'wide')}
                    ${field(T('digestThreshold'), num('digest_threshold', v.digest_threshold, 1, 100))}
                    <div class="wide">${check('root_cause_only', v.root_cause_only, T('rootCauseOnly'))}</div></div>`) +
                section(T('channels'), (v.channels.length ? v.channels.map(channelCard).join('') : `<div class="admin-empty">${esc(T('none'))}</div>`),
                    `<button type="button" class="btn-secondary" data-add="teams">${esc(T('addTeams'))}</button>
                     <button type="button" class="btn-secondary" data-add="webhook">${esc(T('addWebhook'))}</button>
                     <button type="button" class="btn-secondary" data-add="email">${esc(T('addEmail'))}</button>`) +
                section(T('templates'), `${templates}<p class="admin-hint" style="margin-top:.8rem">${esc(T('placeholders'))}：
                    ${data.placeholders.map(p => `<code>{${esc(p)}}</code>`).join(' ')}</p>
                    <div class="admin-form-actions"><button type="submit" class="btn-save">${esc(T('save'))}</button></div>`) +
                `</form>` +
                section(T('deliveries'), `${deliveries.pending_events ? `<p class="admin-hint">${esc(T('pendingEvents', { n: deliveries.pending_events }))}</p>` : ''}
                    ${deliveries.items.length ? `<div class="admin-table-wrap"><table class="admin-table">
                    <tr><th>${esc(T('time'))}</th><th>${esc(T('channel'))}</th><th>${esc(T('title'))}</th><th>${esc(T('status'))}</th><th>${esc(T('attempts'))}</th><th></th></tr>
                    ${deliveries.items.map(d => `<tr><td class="nw mono">${fmtTime(d.sent_at || d.created_at)}</td><td>${esc(d.channel || '')}</td>
                        <td>${esc(d.title || '')}${d.error ? `<div class="admin-hint" style="color:var(--t-crit)">${esc(d.error)}</div>` : ''}</td>
                        <td>${statusBadge(d.status)}</td><td class="num">${d.attempts}</td>
                        <td>${d.status !== 'sent' ? `<button type="button" class="btn-link" data-retry="${d.id}">${esc(T('retry'))}</button>` : ''}</td></tr>`).join('')}
                    </table></div>` : `<div class="admin-empty">${esc(T('none'))}</div>`}`);
            bind();
        };
        const collect = () => {
            const form = document.getElementById('notify-form');
            const raw = formValues(form);
            const v = notifyDraft;
            v.enabled = raw.enabled;
            v.public_url = raw.public_url;
            v.digest_threshold = raw.digest_threshold;
            v.root_cause_only = raw.root_cause_only;
            v.channels.forEach((ch, i) => {
                for (const key of ['name', 'url', 'secret', 'smtp_host', 'smtp_security', 'smtp_user', 'smtp_password', 'mail_from', 'min_severity']) {
                    if (raw[`ch.${i}.${key}`] !== undefined) ch[key] = raw[`ch.${i}.${key}`];
                }
                if (raw[`ch.${i}.smtp_port`] !== undefined) ch.smtp_port = raw[`ch.${i}.smtp_port`];
                if (raw[`ch.${i}.mail_to`] !== undefined) ch.mail_to = raw[`ch.${i}.mail_to`].split(/[,;\s]+/).filter(Boolean);
                ch.enabled = raw[`ch.${i}.enabled`];
                ch.events = EVENTS.filter(ev => raw[`ch.${i}.ev.${ev}`]);
            });
            EVENTS.forEach(ev => {
                v.templates[ev] = { title: raw[`tpl.${ev}.title`], body: raw[`tpl.${ev}.body`] };
            });
            return v;
        };
        const bind = () => {
            const form = document.getElementById('notify-form');
            form.addEventListener('submit', e => {
                e.preventDefault();
                run(e.submitter, () => api('notifications', 'PUT', collect()), T('saved')).then(r => { if (r) tabs.notify(); });
            });
            main.querySelectorAll('[data-add]').forEach(b => b.addEventListener('click', () => {
                collect();
                const id = Math.random().toString(36).slice(2, 10);
                notifyDraft.channels.push({
                    id, type: b.dataset.add, name: b.dataset.add === 'email' ? 'Email' : b.dataset.add === 'teams' ? 'Teams' : 'Webhook',
                    enabled: true, events: ['alert_new', 'alert_resolved', 'health_failed'], min_severity: 'warning',
                    url: '', secret: '', smtp_host: '', smtp_port: 587, smtp_security: 'starttls', smtp_user: '',
                    smtp_password: '', mail_from: '', mail_to: []
                });
                draw();
            }));
            main.querySelectorAll('[data-remove]').forEach(b => b.addEventListener('click', () => {
                collect();
                notifyDraft.channels.splice(Number(b.dataset.remove), 1);
                draw();
            }));
            main.querySelectorAll('[data-reset]').forEach(b => b.addEventListener('click', () => {
                collect();
                notifyDraft.templates[b.dataset.reset] = { ...defaults[b.dataset.reset] };
                draw();
            }));
            main.querySelectorAll('[data-test]').forEach(b => b.addEventListener('click', () => run(b, async () => {
                const ch = collect().channels[Number(b.dataset.test)];
                const r = await api('notifications/test', 'POST', { channel: ch });
                if (!r.ok) throw new Error(r.error);
                toast(T('testSent'));
            })));
            main.querySelectorAll('[data-retry]').forEach(b => b.addEventListener('click', () =>
                run(b, () => api(`notifications/deliveries/${b.dataset.retry}/retry`, 'POST'), T('saved'))));
        };
        draw();
    };

    // Backup & restore
    tabs.backup = async () => {
        const data = await api('backups');
        const s = data.settings;
        const days = [1, 2, 3, 4, 5, 6, 0].map(d => {
            const date = new Date(2024, 0, 1 + ((d + 6) % 7)); // 2024-01-01 is a Monday
            return [(d + 6) % 7, date.toLocaleDateString(i18n.currentLang, { weekday: 'long' })];
        });
        const rows = data.items.map(b => `<tr data-name="${esc(b.name)}">
            <td class="nw mono">${fmtTime(b.time)}<div class="admin-hint">${esc(b.name)}</div></td>
            <td>${esc(T('trig_' + (b.trigger || 'cli')))}${b.by ? ` · <span class="mono">${esc(b.by)}</span>` : ''}</td>
            <td class="num">${fmtBytes(b.size)}</td>
            <td class="nw">
                <button type="button" class="btn-link" data-act="download" ${b.readable ? '' : 'disabled'}>${esc(T('download'))}</button> ·
                <button type="button" class="btn-link" data-act="restore" ${data.runner.available ? '' : 'disabled'}>${esc(T('restore'))}</button> ·
                <button type="button" class="btn-link danger" data-act="delete" ${data.runner.available ? '' : 'disabled'}>${esc(T('deleteAccount'))}</button></td></tr>`).join('');
        main.innerHTML = runnerBanner(data.runner) +
            section(T('backupSchedule'), `<form id="backup-form" class="admin-form">
                <div class="wide">${check('enabled', s.enabled, T('backupEnabled'))}</div>
                ${field(T('frequency'), select('frequency', s.frequency, [['daily', T('daily')], ['weekly', T('weekly')]]))}
                ${field(T('weekday'), select('weekday', s.weekday, days))}
                ${field(T('at'), `<input type="time" name="time" value="${esc(s.time)}" required>`)}
                ${field(T('keep'), num('keep', s.keep, 1, 365))}
                <p class="admin-hint wide">${esc(T('backupHint'))}${data.next_run ? ` ${esc(T('nextRun'))}：${fmtTime(data.next_run)}` : ''}</p>
                <div class="admin-form-actions wide"><button type="submit" class="btn-save">${esc(T('save'))}</button></div></form>`) +
            section(`${T('backups')}（${data.items.length}）`,
                `${data.disk ? `<p class="admin-hint" style="margin-bottom:.6rem"><code>${esc(data.dir)}</code> · ${fmtBytes(data.total_size)} · ${esc(T('diskFree', { free: fmtBytes(data.disk.free), total: fmtBytes(data.disk.total) }))}</p>` : ''}
                 ${rows ? `<div class="admin-table-wrap"><table class="admin-table" id="backup-table">
                    <tr><th>${esc(T('time'))}</th><th>${esc(T('trigger'))}</th><th>${esc(T('size'))}</th><th>${esc(T('actions'))}</th></tr>${rows}</table></div>`
                        : `<div class="admin-empty">${esc(T('none'))}</div>`}`,
                `<button type="button" class="btn-save" id="backup-now" ${data.runner.available ? '' : 'disabled'}>${esc(T('backupNow'))}</button>`);
        const form = document.getElementById('backup-form');
        const syncDay = () => { form.querySelector('[name="weekday"]').closest('.admin-field').hidden = form.querySelector('[name="frequency"]').value !== 'weekly'; };
        form.querySelector('[name="frequency"]').addEventListener('change', syncDay);
        syncDay();
        form.addEventListener('submit', e => {
            e.preventDefault();
            const v = formValues(form);
            v.weekday = Number(v.weekday);
            run(e.submitter, () => api('settings/backup', 'PUT', v), T('saved')).then(() => tabs.backup());
        });
        document.getElementById('backup-now').addEventListener('click', e => run(e.target, async () => {
            const r = await api('backups', 'POST');
            watchJob(r.job, 'backup', () => tabs.backup());
        }));
        main.querySelectorAll('#backup-table tr[data-name]').forEach(tr => {
            const name = tr.dataset.name;
            tr.querySelectorAll('[data-act]').forEach(b => b.addEventListener('click', async () => {
                if (b.dataset.act === 'download') {
                    const v = await modal({
                        title: `${T('download')} ${name}`, okLabel: T('download'),
                        html: `<p class="admin-hint">${esc(T('reauthHint'))}</p>${field(T('yourPassword'), '<input type="password" name="password" autocomplete="current-password">')}`
                    });
                    if (!v) return;
                    await run(b, async () => {
                        const r = await fetch(`${API}/backups/${encodeURIComponent(name)}/download`, {
                            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: v.password })
                        });
                        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
                        const blob = await r.blob();
                        const a = document.createElement('a');
                        a.href = URL.createObjectURL(blob);
                        a.download = name;
                        a.click();
                        setTimeout(() => URL.revokeObjectURL(a.href), 10000);
                    });
                } else if (b.dataset.act === 'restore') {
                    const v = await modal({
                        title: `${T('restore')} ${name}`, okLabel: T('restore'), danger: true,
                        html: `<div class="admin-banner fail">${esc(T('restoreWarn'))}</div>
                            ${field(T('typeToConfirm', { word: 'RESTORE' }), '<input type="text" name="confirm" autocomplete="off">')}
                            ${field(T('yourPassword'), '<input type="password" name="password" autocomplete="current-password">')}`
                    });
                    if (!v) return;
                    const r = await run(b, () => api(`backups/${encodeURIComponent(name)}/restore`, 'POST', v));
                    if (r) watchJob(r.job, 'restore', () => tabs.backup());
                } else {
                    const v = await modal({ title: T('confirmDeleteBackup', { name }), okLabel: T('deleteAccount'), danger: true, html: '' });
                    if (!v) return;
                    const r = await run(b, () => api(`backups/${encodeURIComponent(name)}`, 'DELETE'));
                    if (r) watchJob(r.job, 'delete-backup', () => tabs.backup());
                }
            }));
        });
    };

    // HTTPS certificate
    tabs.tls = async () => {
        const data = await api('tls');
        const c = data.current;
        let info = `<p class="admin-hint">${esc(T('noCertInfo'))}</p>`;
        if (c) {
            const left = c.not_after ? Math.floor((new Date(c.not_after) - new Date()) / 86400000) : null;
            info = `<dl class="admin-kv">
                <dt>${esc(T('subject'))}</dt><dd>${esc(c.subject)} ${c.self_signed ? `<span class="badge warn">${esc(T('selfSigned'))}</span>` : ''}</dd>
                <dt>${esc(T('issuer'))}</dt><dd>${esc(c.issuer)}</dd>
                <dt>${esc(T('san'))}</dt><dd>${esc(c.san || '')}</dd>
                <dt>${esc(T('validFrom'))}</dt><dd>${fmtTime(c.not_before)}</dd>
                <dt>${esc(T('validTo'))}</dt><dd>${fmtTime(c.not_after)} ${left !== null ? `<span class="badge ${left < 0 ? 'fail' : left < 30 ? 'warn' : 'ok'}">${esc(T('daysLeft', { n: left }))}</span>` : ''}</dd>
                <dt>${esc(T('fingerprint'))}</dt><dd>${esc(c.fingerprint || '')}</dd></dl>`;
        }
        main.innerHTML = runnerBanner(data.runner) + section(T('currentCert'), info) +
            section(T('uploadCert'), `<form id="tls-form" class="admin-form">
                ${field(T('certFile'), '<input type="file" name="cert" accept=".pem,.crt,.cer" required>')}
                ${field(T('keyFile'), '<input type="file" name="key" accept=".pem,.key" required>')}
                ${field(T('chainFile'), '<input type="file" name="chain" accept=".pem,.crt,.cer">')}
                <p class="admin-hint wide">${esc(T('tlsHint'))}</p>
                <div class="admin-form-actions wide"><button type="submit" class="btn-save" ${data.runner.available ? '' : 'disabled'}>${esc(T('install'))}</button></div></form>`) +
            section(T('jobs'), jobsTable(data.jobs));
        bindJobLinks(main);
        document.getElementById('tls-form').addEventListener('submit', e => {
            e.preventDefault();
            const read = name => {
                const file = e.target.querySelector(`[name="${name}"]`).files[0];
                return file ? file.text() : Promise.resolve('');
            };
            run(e.submitter, async () => {
                const [cert, key, chain] = await Promise.all([read('cert'), read('key'), read('chain')]);
                const body = await api('tls', 'POST', { cert, key, chain });
                watchJob(body.job, 'tls', () => tabs.tls());
            });
        });
    };

    // System info
    tabs.sysinfo = async () => {
        const d = await api('sysinfo');
        const kv = obj => `<dl class="admin-kv">${Object.entries(obj).map(([k, v]) =>
            `<dt>${esc(k)}</dt><dd>${esc(typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v ?? ''))}</dd>`).join('')}</dl>`;
        const h = d.hardware;
        const hw = {
            hostname: h.hostname, cpu: `${h.cpu} × ${h.cpu_count}`, load: h.load.join(' / '),
            memory: `${fmtBytes(h.memory_available)} free / ${fmtBytes(h.memory_total)}`,
            swap: h.swap_total ? `${fmtBytes(h.swap_free)} free / ${fmtBytes(h.swap_total)}` : '—'
        };
        const os = { ...d.os, uptime_seconds: fmtDuration(d.os.uptime_seconds) };
        const rt = { ...d.runtime, runner: d.runtime.runner.available ? 'available' : (d.runtime.runner.reason || 'unavailable') };
        const db = { version: d.database.version, size: fmtBytes(d.database.size), ...Object.fromEntries(Object.entries(d.database.rows || {}).map(([k, v]) => [`rows.${k}`, v])) };
        const disks = d.disks.length ? `<table class="admin-table"><tr><th></th><th>path</th><th>${esc(T('size'))}</th><th>free</th><th>%</th></tr>
            ${d.disks.map(x => `<tr><td>${esc(x.label)}</td><td class="mono">${esc(x.path)}</td><td class="num">${fmtBytes(x.total)}</td><td class="num">${fmtBytes(x.free)}</td>
                <td class="num"><span class="badge ${x.percent >= 90 ? 'fail' : x.percent >= 80 ? 'warn' : 'ok'}">${x.percent}%</span></td></tr>`).join('')}</table>` : '';
        const services = d.services.length ? `<div class="admin-table-wrap"><table class="admin-table"><tr><th>unit</th><th>${esc(T('status'))}</th><th>enabled</th><th>PID</th><th>since</th><th>memory</th><th>restarts</th></tr>
            ${d.services.map(s => `<tr><td class="mono">${esc(s.unit)}</td><td><span class="badge ${s.ok ? 'ok' : 'fail'}">${esc(s.state)}</span></td><td>${esc(s.enabled)}</td>
                <td class="num">${esc(s.pid)}</td><td class="mono">${esc(s.since)}</td><td class="num">${fmtBytes(s.memory)}</td><td class="num">${esc(s.restarts)}</td></tr>`).join('')}</table></div>` : `<p class="admin-hint">—</p>`;
        const pk = d.packages;
        const packages = `<p class="admin-hint" style="margin-bottom:.6rem">${esc(d.package_mismatches ? T('mismatch', { v: d.package_mismatches }) : T('packagesOk'))}</p>
            <div class="admin-table-wrap"><table class="admin-table"><tr><th></th><th>version</th><th></th><th>license</th><th></th></tr>
            ${pk.map(p => `<tr><td class="mono">${esc(p.name)}</td><td class="mono">${esc(p.version)}${p.mismatch ? ` <span class="badge fail">${esc(T('mismatch', { v: p.required }))}</span>` : ''}</td>
                <td>${esc(T(p.direct ? 'direct' : 'indirect'))}</td><td>${esc(p.license)}</td><td class="admin-hint">${esc(p.summary)}</td></tr>`).join('')}</table></div>`;
        main.innerHTML =
            `<p class="admin-hint">${esc(T('generatedIn', { at: d.generated_at, s: d.elapsed }))}</p>` +
            section(T('hardware'), kv(hw), `<button type="button" class="btn-secondary" id="sys-refresh">${esc(T('refresh'))}</button>
                <button type="button" class="btn-secondary" id="sys-json">${esc(T('downloadJson'))}</button>`) +
            section(T('osSection'), kv(os)) + section(T('runtime'), kv(rt)) +
            section(T('database'), kv(db)) + section(T('disks'), disks || '—') +
            section(T('services'), services) + section(T('configSection'), kv(d.config)) +
            section(`${T('packages')}（${pk.length}）`, packages);
        document.getElementById('sys-refresh').addEventListener('click', e => run(e.target, () => tabs.sysinfo()));
        document.getElementById('sys-json').addEventListener('click', () => {
            const a = document.createElement('a');
            a.href = URL.createObjectURL(new Blob([JSON.stringify(d, null, 2)], { type: 'application/json' }));
            a.download = `mf-topomon-sysinfo-${d.hardware.hostname}.json`;
            a.click();
        });
    };

    // Version & upgrade
    tabs.version = async () => {
        const data = await api('version');
        const c = data.current;
        const u = data.update;
        const commits = list => `<table class="admin-table">${list.map(x => `<tr><td class="mono nw">${esc(x.sha)}</td><td class="nw mono">${fmtTime(x.date)}</td><td>${esc(x.subject)}</td></tr>`).join('')}</table>`;
        let updateInfo = `<p class="admin-hint">${esc(T('neverChecked'))}</p>`;
        if (u && u.checked_at) {
            const behind = (u.behind || []).length;
            updateInfo = behind
                ? `<div class="admin-banner warn">${esc(T('behindBy', { n: behind, at: fmtTime(u.checked_at) }))}</div>${commits(u.behind)}`
                : `<div class="admin-banner ok">${esc(T('upToDate', { at: fmtTime(u.checked_at) }))}</div>`;
        }
        main.innerHTML = runnerBanner(data.runner) +
            section(T('currentVersion'), c ? `<dl class="admin-kv">
                <dt>${esc(T('commit'))}</dt><dd>${esc(c.commit)} · ${fmtTime(c.date)}</dd>
                <dt>${esc(T('branch'))}</dt><dd>${esc(c.branch)}</dd>
                <dt>${esc(T('lastGood'))}</dt><dd>${esc(c.last_good || '')}</dd>
                <dt>remote</dt><dd>${esc(c.remote || '')}</dd></dl>
                <h3 class="admin-hint" style="margin:1rem 0 .3rem">${esc(T('recentChanges'))}</h3>${commits(c.recent || [])}` : `<p class="admin-hint">—</p>`) +
            section(T('upgradeNow'), `${updateInfo}<p class="admin-hint" style="margin-top:.8rem">${esc(T('upgradeHint'))}</p>`,
                `<button type="button" class="btn-secondary" id="update-check" ${data.runner.available ? '' : 'disabled'}>${esc(T('checkUpdates'))}</button>
                 <button type="button" class="btn-save" id="upgrade-run" ${data.runner.available ? '' : 'disabled'}>${esc(T('upgradeNow'))}</button>`) +
            section(T('jobs'), jobsTable(data.jobs));
        bindJobLinks(main);
        document.getElementById('update-check').addEventListener('click', e => run(e.target, async () => {
            const r = await api('upgrade/check', 'POST');
            watchJob(r.job, 'check-update', () => tabs.version());
        }));
        document.getElementById('upgrade-run').addEventListener('click', async e => {
            const v = await modal({
                title: T('upgradeNow'), okLabel: T('upgradeNow'), danger: true,
                html: `<p class="admin-hint">${esc(T('upgradeHint'))}</p>${field(T('typeToConfirm', { word: 'UPGRADE' }), '<input type="text" name="confirm" autocomplete="off">')}`
            });
            if (!v) return;
            const r = await run(e.target, () => api('upgrade', 'POST', v));
            if (r) watchJob(r.job, 'upgrade', () => tabs.version());
        });
    };

    // -- routing ---------------------------------------------------------------

    async function show() {
        const name = (location.hash || '#status').slice(1);
        const tab = tabs[name] ? name : 'status';
        document.querySelectorAll('#admin-tabs a').forEach(a => a.classList.toggle('active', a.dataset.tab === tab));
        main.innerHTML = `<div class="admin-empty">${esc(T('loading'))}</div>`;
        try {
            await tabs[tab]();
        } catch (err) {
            main.innerHTML = `<div class="admin-banner fail">${esc(err.message || String(err))}</div>`;
        }
        i18n.applyTranslations();
    }

    window.addEventListener('hashchange', show);
    document.addEventListener('DOMContentLoaded', () => {
        i18n.init();
        show();
    });
    window.adminTabs = tabs;
})();
