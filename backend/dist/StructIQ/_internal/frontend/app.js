// ================================================================
//  StructIQ — App Logic
// ================================================================

// ─────────────────────────────────────────────────────────────────
//  AUTH MODULE
// ─────────────────────────────────────────────────────────────────
const AUTH_KEY = 'siq_token';

function authToken() { return localStorage.getItem(AUTH_KEY); }

/** Wrapper around fetch() that injects the Bearer token automatically. */
async function authFetch(url, options = {}) {
  const token = authToken();
  const headers = Object.assign({}, options.headers || {});
  if (token) headers['Authorization'] = 'Bearer ' + token;
  return fetch(url, { ...options, headers });
}

async function initAuth() {
  const token = authToken();
  if (!token) { showAuthOverlay(); return; }

  try {
    const res = await fetch('/api/auth/me', {
      headers: { 'Authorization': 'Bearer ' + token }
    });
    if (res.ok) {
      const user = await res.json();
      hideAuthOverlay(user);
    } else {
      localStorage.removeItem(AUTH_KEY);
      showAuthOverlay();
    }
  } catch {
    // Network error – still let them in if token exists (offline ETABS use)
    hideAuthOverlay(null);
  }
}

function showAuthOverlay() {
  document.getElementById('auth-overlay').classList.remove('hidden');
  // Reset button states so they're always ready when the overlay re-appears
  // (after logout the button is still disabled from the previous successful login)
  const loginBtn = document.querySelector('#form-login .auth-submit-btn');
  if (loginBtn) { loginBtn.disabled = false; loginBtn.textContent = 'Sign In'; }
  const regBtn = document.querySelector('#form-register .auth-submit-btn');
  if (regBtn) { regBtn.disabled = false; regBtn.textContent = 'Create Account'; }
}

function hideAuthOverlay(user) {
  document.getElementById('auth-overlay').classList.add('hidden');
  if (user) renderUserPill(user);
  bootApp();
  // Fire-and-forget cloud plan sync (updates plan badge if Pro/Enterprise)
  syncCloudPlan();
}

function renderUserPill(user) {
  const pill = document.getElementById('user-pill');
  pill.classList.remove('hidden');

  // Avatar: first letter of name
  document.getElementById('user-avatar').textContent = (user.name || user.email || '?')[0].toUpperCase();

  // Name
  document.getElementById('user-pill-name').textContent = user.name || user.email;

  // Store email for Stripe checkout
  currentUserEmail = user.email || '';

  // Plan badge + nav locks
  currentUserPlan = user.plan || 'free';
  updatePlanBadge(currentUserPlan);
}

/** Update just the plan badge chip (called by renderUserPill + syncCloudPlan). */
function updatePlanBadge(plan) {
  const planEl = document.getElementById('user-pill-plan');
  if (!planEl) return;
  planEl.textContent = plan;
  planEl.className = 'user-pill-plan';
  if (plan === 'pro')        planEl.classList.add('plan-pro');
  if (plan === 'enterprise') planEl.classList.add('plan-enterprise');
  // Show "Manage Subscription" button only for paid plans
  const manageBtn = document.getElementById('btn-manage-sub');
  if (manageBtn) {
    if (plan === 'pro' || plan === 'enterprise') {
      manageBtn.classList.remove('hidden');
    } else {
      manageBtn.classList.add('hidden');
    }
  }
  // Keep global plan state in sync and refresh nav locks
  currentUserPlan = plan;
  updateNavLocks(plan);
}

/** Open the Lemon Squeezy customer portal so the user can manage their subscription. */
async function openBillingPortal() {
  const btn = document.getElementById('btn-manage-sub');
  if (btn) { btn.disabled = true; btn.title = 'Loading…'; }
  try {
    const res = await authFetch('/api/billing/portal');
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Could not load billing portal', 'error');
      return;
    }
    if (data.portal_url) {
      window.open(data.portal_url, '_blank');
    } else {
      showToast('No portal URL returned', 'error');
    }
  } catch (err) {
    showToast('Could not reach billing server', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.title = 'Manage subscription'; }
  }
}

/**
 * Silently call /api/cloud/sync after login.
 * If Railway responds with a plan, update the badge — otherwise keep local value.
 * Also checks whether this session was kicked by another login (session_valid).
 */
async function syncCloudPlan() {
  try {
    const res = await authFetch('/api/cloud/sync');
    if (!res.ok) return;
    const data = await res.json();
    // Only upgrade the plan — never let a cloud "free" response downgrade a
    // locally-granted enterprise/pro plan (e.g. admin accounts).
    if (data.plan && (PLAN_LEVEL[data.plan] || 0) >= (PLAN_LEVEL[currentUserPlan] || 0)) {
      updatePlanBadge(data.plan);
    }
    // Show a subtle indicator if synced from cloud
    if (data.synced && data.source === 'cloud') {
      const planEl = document.getElementById('user-pill-plan');
      if (planEl) planEl.title = `Plan synced from cloud ✓`;
    }
    // Another login kicked this session out → notify and force-logout
    if (data.session_valid === false) {
      showToast('Your session was ended — you signed in on another device.', 'error');
      setTimeout(forceLogout, 2500);
    }
  } catch { /* Network error — silently ignore, app works fully offline */ }
}

/** Force-logout: clear local token, hide user pill, return to login screen. */
function forceLogout() {
  localStorage.removeItem(AUTH_KEY);
  document.getElementById('user-pill').classList.add('hidden');
  document.getElementById('login-email').value    = '';
  document.getElementById('login-password').value = '';
  document.getElementById('login-error').classList.add('hidden');
  showAuthOverlay();
}

function bootApp() {
  // Kick off background tasks that need auth
  checkStatus();
  populateReactionCombos();
  pmmInitRebarTable();  // fetch SI rebar table now that token is available
}

// ── State ──
let isConnected          = false;
let driftsChartHasData   = false;       // true when Plotly chart is rendered
let driftCombos          = [];          // available combinations
let driftCases           = [];          // available load cases
let driftSelected        = new Map();   // name → 'combo' | 'case'
let driftFilterText      = '';
let driftSourceFilter    = 'all';       // 'all' | 'combo' | 'case'
let driftLastData        = [];          // raw rows from last Extract (for table)
let reactionsData         = [];          // ALL fetched reactions (unfiltered)
let reactionsChartHasData = false;       // true when Plotly chart is rendered

// ── Joint / Spring Reactions state ──
let jointData           = [];           // ALL fetched joint reactions (unfiltered)
let jointLoadType       = 'combo';      // 'combo' | 'case'
let jointAllCombos      = [];           // master list for picker
let jointSelectedCombos = new Set();    // which items are checked
let jointActiveForce    = 'FZ';         // component shown in bubble plot
let jointBubbleCombo    = '';           // combo/case shown in bubble plot
// ── Bubble plot display controls ──
let jointBubbleScale = 1.0;            // bubble size multiplier (0.25 – 4.0)
let jointTextSize    = 11;             // label font size in px (7 – 20)
let jointAxisX       = { min: null, max: null };  // null = autorange
let jointAxisY       = { min: null, max: null };  // null = autorange
let jointColorMin    = null;           // color scale min  (null = auto = data min)
let jointColorMax    = null;           // color scale max  (null = auto = data max)
let jointColorPalette = 'Plasma';     // active Plotly named colorscale
let reactionsActiveForces = new Set(['FX','FY','FZ','MX','MY','MZ']);
let reactionsAllCombos      = [];         // master list of available picker names
let reactionsSelectedCombos = new Set();  // which items are checked (empty = all)
let reactionsLoadType       = 'combo';    // 'combo' | 'case'

// ── Plan / license state ──
const PLAN_LEVEL = { free: 0, pro: 1, enterprise: 2 };
let currentUserPlan  = 'free';
let currentUserEmail = '';        // stored at login, used by Stripe checkout

/** True if the current user's plan meets or exceeds `required`. */
function userHasPlan(required) {
  return (PLAN_LEVEL[currentUserPlan] || 0) >= (PLAN_LEVEL[required] || 0);
}

/** Show/hide the PRO lock styling on nav items based on current plan. */
function updateNavLocks(plan) {
  document.querySelectorAll('.nav-item[data-plan]').forEach(item => {
    const required = item.dataset.plan || 'free';
    const locked   = !userHasPlan(required);
    item.classList.toggle('locked', locked);
  });
}

/** Show the upgrade-required modal for a named feature. */
function showUpgradeModal(featureName, requiredPlan = 'pro') {
  document.getElementById('upgrade-feat-name').textContent = featureName || 'This feature';
  const descEl = document.getElementById('upgrade-req-desc');
  if (descEl) {
    const label = requiredPlan === 'enterprise' ? 'ENTERPRISE' : 'PRO';
    descEl.innerHTML = `requires a <strong>${label}</strong> plan.`;
  }
  setUpgradeTab(requiredPlan === 'enterprise' ? 'enterprise' : 'pro');
  document.getElementById('upgrade-modal').classList.remove('hidden');
}

/** Switch the upgrade modal between 'pro' and 'enterprise' tabs. */
function setUpgradeTab(tab) {
  document.querySelectorAll('.upgrade-tab-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.upgrade-plan-panel').forEach(p =>
    p.classList.toggle('hidden', p.dataset.tab !== tab));
  const subBtn = document.getElementById('btn-subscribe');
  const entBtn = document.getElementById('btn-contact-enterprise');
  if (subBtn) subBtn.classList.toggle('hidden', tab === 'enterprise');
  if (entBtn) entBtn.classList.toggle('hidden', tab !== 'enterprise');
}

/** Show the grace-period-expired modal. */
function showGraceModal(days, msg) {
  const message = msg ||
    `Your license has not been verified for ${days} days (grace period: ${config.OFFLINE_GRACE_DAYS} days).`;
  document.getElementById('grace-msg').textContent = message;
  document.getElementById('grace-modal').classList.remove('hidden');
}

/** Open a Lemon Squeezy Checkout session for the selected interval (monthly | yearly). */
async function openStripeCheckout(interval) {
  const btn = document.getElementById('btn-subscribe');

  // Disable button while we create the session
  if (btn) { btn.disabled = true; btn.textContent = 'Opening checkout…'; }

  try {
    // Route through local backend — it proxies to Railway and handles auth.
    // Railway will auto-create the user record if it doesn't exist yet.
    const res = await authFetch('/api/stripe/checkout', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ interval }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Failed to create checkout session');

    // Open Lemon Squeezy Checkout in the user's default browser
    if (data.checkout_url) {
      window.open(data.checkout_url, '_blank');
      document.getElementById('upgrade-modal').classList.add('hidden');
      showToast('Checkout opened in your browser!', 'success');
    }
  } catch (err) {
    showToast('Could not start checkout: ' + err.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Subscribe Now'; }
  }
}

// ── Force component color palette (used by Plotly chart) ──
const FORCE_COLORS = {
  FX: '#ef4444', FY: '#f97316', FZ: '#3b82f6',
  MX: '#8b5cf6', MY: '#a855f7', MZ: '#6366f1',
};

// ── Boot ──
document.addEventListener('DOMContentLoaded', () => {
  // ── Auth overlay setup ──
  initAuth();

  // Tab switching: Sign In ↔ Create Account
  document.getElementById('tab-login').addEventListener('click', () => {
    document.getElementById('tab-login').classList.add('active');
    document.getElementById('tab-register').classList.remove('active');
    document.getElementById('form-login').classList.remove('hidden');
    document.getElementById('form-register').classList.add('hidden');
    document.getElementById('login-error').classList.add('hidden');
  });
  document.getElementById('tab-register').addEventListener('click', () => {
    document.getElementById('tab-register').classList.add('active');
    document.getElementById('tab-login').classList.remove('active');
    document.getElementById('form-register').classList.remove('hidden');
    document.getElementById('form-login').classList.add('hidden');
    document.getElementById('reg-error').classList.add('hidden');
  });

  // Login form submit
  document.getElementById('form-login').addEventListener('submit', async e => {
    e.preventDefault();
    const btn = e.target.querySelector('.auth-submit-btn');
    const errEl = document.getElementById('login-error');
    errEl.classList.add('hidden');
    btn.disabled = true; btn.textContent = 'Signing in…';
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: document.getElementById('login-email').value.trim(),
          password: document.getElementById('login-password').value,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Login failed');
      localStorage.setItem(AUTH_KEY, data.token);
      hideAuthOverlay(data.user);
    } catch (err) {
      errEl.textContent = err.message;
      errEl.classList.remove('hidden');
      btn.disabled = false; btn.textContent = 'Sign In';
    }
  });

  // Register form submit
  document.getElementById('form-register').addEventListener('submit', async e => {
    e.preventDefault();
    const btn = e.target.querySelector('.auth-submit-btn');
    const errEl = document.getElementById('reg-error');
    errEl.classList.add('hidden');
    btn.disabled = true; btn.textContent = 'Creating account…';
    try {
      const res = await fetch('/api/auth/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: document.getElementById('reg-email').value.trim(),
          name:  document.getElementById('reg-name').value.trim(),
          password: document.getElementById('reg-password').value,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Registration failed');
      localStorage.setItem(AUTH_KEY, data.token);
      hideAuthOverlay(data.user);
    } catch (err) {
      errEl.textContent = err.message;
      errEl.classList.remove('hidden');
      btn.disabled = false; btn.textContent = 'Create Account';
    }
  });

  // Logout
  document.getElementById('btn-logout').addEventListener('click', async () => {
    const token = authToken();
    if (token) {
      await fetch('/api/auth/logout', {
        method: 'POST',
        headers: { 'Authorization': 'Bearer ' + token },
      }).catch(() => {});
    }
    localStorage.removeItem(AUTH_KEY);
    // Clear user pill, show overlay
    document.getElementById('user-pill').classList.add('hidden');
    document.getElementById('login-email').value = '';
    document.getElementById('login-password').value = '';
    document.getElementById('login-error').classList.add('hidden');
    showAuthOverlay();
  });

  // Navigation
  document.querySelectorAll('.nav-item').forEach(btn => {
    btn.addEventListener('click', e => {
      e.preventDefault();
      // Plan gate: show upgrade modal if feature requires a higher plan
      const requiredPlan = btn.dataset.plan || 'free';
      if (!userHasPlan(requiredPlan)) {
        const label = btn.textContent.trim().replace(/PRO|ENTERPRISE/g, '').trim();
        showUpgradeModal(label, requiredPlan);
        return;
      }
      document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById(btn.dataset.target).classList.add('active');
      // Auto-load combo/case list when joint panel is opened (if not already loaded)
      if (btn.dataset.target === 'joint-panel' && isConnected && !jointAllCombos.length) {
        jointLoadSources();
      }
    });
  });

  // Upgrade modal close / dismiss
  ['btn-upgrade-close', 'btn-upgrade-dismiss'].forEach(id =>
    document.getElementById(id).addEventListener('click', () =>
      document.getElementById('upgrade-modal').classList.add('hidden')));

  // Upgrade modal tab switching (PRO | ENTERPRISE)
  document.querySelectorAll('.upgrade-tab-btn').forEach(btn =>
    btn.addEventListener('click', () => setUpgradeTab(btn.dataset.tab)));

  // PRO → Stripe subscribe
  document.getElementById('btn-subscribe').addEventListener('click', () => {
    const interval = document.querySelector('input[name="upgrade-interval"]:checked')?.value || 'monthly';
    openStripeCheckout(interval);
  });

  // Enterprise → contact email
  document.getElementById('btn-contact-enterprise').addEventListener('click', () => {
    window.open(
      'mailto:mmi.structural@gmail.com?subject=StructIQ%20Enterprise%20Inquiry',
      '_blank'
    );
    document.getElementById('upgrade-modal').classList.add('hidden');
  });

  // Grace modal close
  ['btn-grace-close', 'btn-grace-dismiss'].forEach(id =>
    document.getElementById(id).addEventListener('click', () =>
      document.getElementById('grace-modal').classList.add('hidden')));

  // Manage subscription (Lemon Squeezy customer portal)
  document.getElementById('btn-manage-sub').addEventListener('click', openBillingPortal);

  // Core buttons
  document.getElementById('btn-reconnect').addEventListener('click', checkStatus);
  document.getElementById('btn-get-torsion').addEventListener('click', getTorsion);
  document.getElementById('btn-get-reactions').addEventListener('click', getReactions);

  // Drift panel buttons
  document.getElementById('btn-get-drift-sources').addEventListener('click', driftGetSources);
  document.getElementById('btn-extract-drift').addEventListener('click', driftExtract);

  // ── Drift panel: tab switching (same pattern as reactions / joint) ──
  document.querySelectorAll('#drifts-panel .tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#drifts-panel .tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('#drifts-panel .tab-panel').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      const panel = document.getElementById(btn.dataset.tab);
      if (panel) panel.classList.add('active');
      // Re-trigger Plotly resize when switching to chart tab
      if (btn.dataset.tab === 'drift-chart-tab' && driftsChartHasData) {
        setTimeout(() => Plotly.Plots.resize('driftsChart'), 50);
      }
    });
  });

  // ── Drift picker: open / close ──
  document.getElementById('drift-picker-btn').addEventListener('click', e => {
    e.stopPropagation();
    const dd     = document.getElementById('drift-picker-dropdown');
    const btn    = document.getElementById('drift-picker-btn');
    const isOpen = !dd.classList.contains('hidden');
    dd.classList.toggle('hidden', isOpen);
    btn.classList.toggle('open', !isOpen);
    if (!isOpen) {
      const si = document.getElementById('drift-picker-search');
      si.value = '';
      driftPickerApplyFilter('');
      setTimeout(() => si.focus(), 60);
    }
  });
  document.getElementById('drift-picker-search').addEventListener('click', e => e.stopPropagation());
  document.getElementById('drift-picker-search').addEventListener('input', e =>
    driftPickerApplyFilter(e.target.value));

  // ── Drift picker: Select All / Clear All ──
  document.getElementById('drift-pick-all').addEventListener('click', () => {
    document.querySelectorAll('#drift-picker-list .combo-picker-item').forEach(item => {
      if (item.style.display === 'none') return;
      const cb = item.querySelector('input[type="checkbox"]');
      cb.checked = true;
      item.classList.add('checked');
      driftSelected.set(item.dataset.name, item.dataset.dtype);
    });
    driftUpdatePickerLabel();
  });
  document.getElementById('drift-pick-none').addEventListener('click', () => {
    document.querySelectorAll('#drift-picker-list .combo-picker-item').forEach(item => {
      const cb = item.querySelector('input[type="checkbox"]');
      cb.checked = false;
      item.classList.remove('checked');
      driftSelected.delete(item.dataset.name);
    });
    driftUpdatePickerLabel();
  });

  // LC spreadsheet buttons
  document.getElementById('btn-get-lc').addEventListener('click', lcFetchCases);
  document.getElementById('btn-clear-lc').addEventListener('click', lcClearCases);
  document.getElementById('btn-filter-lc').addEventListener('click', lcShowFilterModal);
  document.getElementById('btn-generate-batch').addEventListener('click', lcGenerateBatch);
  document.getElementById('btn-add-lc-row').addEventListener('click', () => lcAddRow());
  document.getElementById('btn-filter-apply').addEventListener('click', lcApplyFilter);
  document.getElementById('btn-filter-cancel').addEventListener('click', () =>
    document.getElementById('lc-filter-modal').classList.add('hidden'));
  document.getElementById('btn-filter-all').addEventListener('click', () =>
    document.querySelectorAll('#lc-filter-checks input[type=checkbox]').forEach(cb => cb.checked = true));
  document.getElementById('btn-filter-none').addEventListener('click', () =>
    document.querySelectorAll('#lc-filter-checks input[type=checkbox]').forEach(cb => cb.checked = false));

  // ── Reactions tab switching ──
  document.querySelectorAll('#reactions-panel .tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#reactions-panel .tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('#reactions-panel .tab-panel').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      const panel = document.getElementById(btn.dataset.tab);
      if (panel) panel.classList.add('active');
      // Auto-render chart when switching to chart tab
      if (btn.dataset.tab === 'reactions-chart-tab' && reactionsData.length) {
        renderReactionsChart(reactionsGetFiltered()); // Plotly.react handles both create & update
      }
    });
  });

  // ── Force toggle pills ──
  document.querySelectorAll('.force-toggle').forEach(btn => {
    btn.addEventListener('click', () => {
      const force = btn.dataset.force;
      if (reactionsActiveForces.has(force)) {
        if (reactionsActiveForces.size > 1) {   // keep at least one active
          reactionsActiveForces.delete(force);
          btn.classList.remove('active');
        }
      } else {
        reactionsActiveForces.add(force);
        btn.classList.add('active');
      }
      if (reactionsData.length) renderReactionsChart(reactionsGetFiltered());
    });
  });

  // ── Drift source type filter (All | Combos | Cases) ──
  document.querySelectorAll('.drift-type-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      if (btn.dataset.dtype === driftSourceFilter) return;
      document.querySelectorAll('.drift-type-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      driftSourceFilter = btn.dataset.dtype;
      driftPickerApplyFilter(document.getElementById('drift-picker-search').value);
    });
  });

  // ── Source type toggle (Combinations | Load Cases) ──
  document.querySelectorAll('.react-type-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      if (btn.dataset.type === reactionsLoadType) return; // no change
      document.querySelectorAll('.react-type-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      reactionsLoadType = btn.dataset.type;
      // Reset picker & data when type switches
      reactionsAllCombos = [];
      reactionsSelectedCombos = new Set();
      reactionsData = [];
      const list = document.getElementById('combo-picker-list');
      const label = document.getElementById('combo-picker-label');
      const placeholder = document.getElementById('combo-picker-search');
      list.innerHTML  = `<div class="combo-picker-empty">Click Fetch to load ${reactionsLoadType === 'case' ? 'load cases' : 'combinations'}</div>`;
      label.textContent = reactionsLoadType === 'case' ? 'All Load Cases' : 'All Combinations';
      if (placeholder) placeholder.placeholder = reactionsLoadType === 'case' ? 'Search load cases…' : 'Search combinations…';
      // Clear table & chart
      renderReactionsTableDOM([]);
      const chartDiv = document.getElementById('reactionsChart');
      if (chartDiv) { chartDiv.style.display = 'none'; Plotly.purge('reactionsChart'); }
      document.getElementById('reactions-chart-empty').style.display = '';
      reactionsChartHasData = false;
    });
  });

  // ── Joint reactions: main button ──
  document.getElementById('btn-get-joint-reactions').addEventListener('click', getJointReactions);

  // ── Joint reactions: source type toggle ──
  document.querySelectorAll('.joint-type-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      if (btn.dataset.jtype === jointLoadType) return;
      document.querySelectorAll('.joint-type-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      jointLoadType = btn.dataset.jtype;
      // Reset picker & data
      jointAllCombos = []; jointSelectedCombos = new Set(); jointData = [];
      const list      = document.getElementById('joint-picker-list');
      const label     = document.getElementById('joint-picker-label');
      const srch      = document.getElementById('joint-picker-search');
      const sel       = document.getElementById('joint-bubble-combo');
      list.innerHTML  = `<div class="combo-picker-empty">Loading…</div>`;
      label.textContent = jointLoadType === 'case' ? 'All Load Cases' : 'All Combinations';
      if (srch) srch.placeholder = jointLoadType === 'case' ? 'Search load cases…' : 'Search combinations…';
      sel.innerHTML   = '<option value="">— Fetch data first —</option>';
      renderJointTable([]);
      document.getElementById('jointBubbleChart').style.display = 'none';
      Plotly.purge('jointBubbleChart');
      document.getElementById('joint-bubble-empty').style.display = '';
      // Auto-load sources for new type
      if (isConnected) jointLoadSources();
    });
  });

  // ── Joint picker: open / close ──
  document.getElementById('joint-picker-btn').addEventListener('click', e => {
    e.stopPropagation();
    const dd  = document.getElementById('joint-picker-dropdown');
    const isOpen = !dd.classList.contains('hidden');
    dd.classList.toggle('hidden', isOpen);
    document.getElementById('joint-picker-btn').classList.toggle('open', !isOpen);
    if (!isOpen) {
      const si = document.getElementById('joint-picker-search');
      si.value = '';
      jointPickerApplySearch('');
      setTimeout(() => si.focus(), 60);
    }
  });

  // ── Joint picker: live search ──
  document.getElementById('joint-picker-search').addEventListener('input', e => {
    jointPickerApplySearch(e.target.value);
  });
  document.getElementById('joint-picker-search').addEventListener('click', e => e.stopPropagation());

  // ── Joint picker: Select All / Clear All ──
  document.getElementById('joint-pick-all').addEventListener('click', () => {
    document.querySelectorAll('#joint-picker-list .combo-picker-item').forEach(item => {
      const cb = item.querySelector('input[type="checkbox"]');
      cb.checked = true; item.classList.add('checked');
      jointSelectedCombos.add(cb.value);
    });
    updateJointPickerLabel();
    applyJointFilter();
  });
  document.getElementById('joint-pick-none').addEventListener('click', () => {
    document.querySelectorAll('#joint-picker-list .combo-picker-item').forEach(item => {
      const cb = item.querySelector('input[type="checkbox"]');
      cb.checked = false; item.classList.remove('checked');
    });
    jointSelectedCombos.clear();
    updateJointPickerLabel();
    applyJointFilter();
  });

  // ── Joint panel: tab switching ──
  document.querySelectorAll('#joint-panel .tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#joint-panel .tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('#joint-panel .tab-panel').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      const panel = document.getElementById(btn.dataset.tab);
      if (panel) panel.classList.add('active');
      if (btn.dataset.tab === 'joint-bubble-tab' && jointData.length && jointBubbleCombo) {
        renderJointBubblePlot(jointData, jointActiveForce, jointBubbleCombo);
      }
    });
  });

  // ── Joint force component pills ──
  document.querySelectorAll('.joint-force-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.joint-force-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      jointActiveForce = btn.dataset.jforce;
      if (jointData.length && jointBubbleCombo) {
        renderJointBubblePlot(jointData, jointActiveForce, jointBubbleCombo);
      }
    });
  });

  // ── Joint bubble combo/case selector ──
  document.getElementById('joint-bubble-combo').addEventListener('change', e => {
    jointBubbleCombo = e.target.value;
    if (jointData.length && jointBubbleCombo) {
      renderJointBubblePlot(jointData, jointActiveForce, jointBubbleCombo);
    }
  });

  // ── Bubble display controls ──
  function replotBubble() {
    if (jointData.length && jointBubbleCombo)
      renderJointBubblePlot(jointData, jointActiveForce, jointBubbleCombo);
  }

  // Bubble size slider + ± buttons
  const bsSlider = document.getElementById('bubble-size-slider');
  const bsVal    = document.getElementById('bubble-size-val');
  function applyBubbleScale(v) {
    jointBubbleScale = Math.min(4, Math.max(0.25, +v));
    bsSlider.value   = jointBubbleScale;
    // Format: "1×", "0.5×", "2.5×" — strip trailing .00 or trailing 0 after decimal
    const label = jointBubbleScale % 1 === 0
      ? jointBubbleScale + '×'
      : jointBubbleScale.toFixed(2).replace(/0+$/, '').replace(/\.$/, '') + '×';
    bsVal.textContent = label;
    replotBubble();
  }
  bsSlider.addEventListener('input', e => applyBubbleScale(e.target.value));
  document.getElementById('bubble-size-minus').addEventListener('click', () =>
    applyBubbleScale(+(jointBubbleScale - 0.25).toFixed(2)));
  document.getElementById('bubble-size-plus').addEventListener('click', () =>
    applyBubbleScale(+(jointBubbleScale + 0.25).toFixed(2)));

  // Text size slider + ± buttons
  const btSlider = document.getElementById('bubble-text-slider');
  const btVal    = document.getElementById('bubble-text-val');
  function applyTextSize(v) {
    jointTextSize    = Math.min(20, Math.max(7, Math.round(+v)));
    btSlider.value   = jointTextSize;
    btVal.textContent = jointTextSize + 'px';
    replotBubble();
  }
  btSlider.addEventListener('input', e => applyTextSize(e.target.value));
  document.getElementById('bubble-text-minus').addEventListener('click', () => applyTextSize(jointTextSize - 1));
  document.getElementById('bubble-text-plus').addEventListener('click',  () => applyTextSize(jointTextSize + 1));

  // Axis range inputs — fires on blur/Enter (not every keystroke to avoid thrashing)
  function readAxisRanges() {
    const xminV = document.getElementById('bubble-xmin').value.trim();
    const xmaxV = document.getElementById('bubble-xmax').value.trim();
    const yminV = document.getElementById('bubble-ymin').value.trim();
    const ymaxV = document.getElementById('bubble-ymax').value.trim();
    jointAxisX = { min: xminV !== '' ? +xminV : null, max: xmaxV !== '' ? +xmaxV : null };
    jointAxisY = { min: yminV !== '' ? +yminV : null, max: ymaxV !== '' ? +ymaxV : null };
    replotBubble();
  }
  ['bubble-xmin','bubble-xmax','bubble-ymin','bubble-ymax'].forEach(id => {
    const el = document.getElementById(id);
    el.addEventListener('change', readAxisRanges);   // fires on blur after value change
    el.addEventListener('keydown', e => { if (e.key === 'Enter') { e.target.blur(); readAxisRanges(); } });
  });

  // Reset X / Reset Y buttons
  document.getElementById('bubble-x-reset').addEventListener('click', () => {
    document.getElementById('bubble-xmin').value = '';
    document.getElementById('bubble-xmax').value = '';
    jointAxisX = { min: null, max: null };
    replotBubble();
  });
  document.getElementById('bubble-y-reset').addEventListener('click', () => {
    document.getElementById('bubble-ymin').value = '';
    document.getElementById('bubble-ymax').value = '';
    jointAxisY = { min: null, max: null };
    replotBubble();
  });

  // Color scale min / max inputs
  function readColorRange() {
    const cminV = document.getElementById('bubble-cmin').value.trim();
    const cmaxV = document.getElementById('bubble-cmax').value.trim();
    jointColorMin = cminV !== '' ? +cminV : null;
    jointColorMax = cmaxV !== '' ? +cmaxV : null;
    replotBubble();
  }
  ['bubble-cmin', 'bubble-cmax'].forEach(id => {
    const el = document.getElementById(id);
    el.addEventListener('change', readColorRange);
    el.addEventListener('keydown', e => { if (e.key === 'Enter') { e.target.blur(); readColorRange(); } });
  });
  document.getElementById('bubble-c-reset').addEventListener('click', () => {
    document.getElementById('bubble-cmin').value = '';
    document.getElementById('bubble-cmax').value = '';
    jointColorMin = null;
    jointColorMax = null;
    replotBubble();
  });

  // Palette selector
  document.getElementById('bubble-palette').addEventListener('change', e => {
    jointColorPalette = e.target.value;
    replotBubble();
  });

  // ── Combo picker: open / close ──
  document.getElementById('combo-picker-btn').addEventListener('click', e => {
    e.stopPropagation();
    const dd     = document.getElementById('combo-picker-dropdown');
    const btn    = document.getElementById('combo-picker-btn');
    const isOpen = !dd.classList.contains('hidden');
    dd.classList.toggle('hidden', isOpen);
    btn.classList.toggle('open', !isOpen);
    if (!isOpen) {                       // just opened — reset search & focus it
      const si = document.getElementById('combo-picker-search');
      si.value = '';
      comboPickerApplySearch('');
      setTimeout(() => si.focus(), 60);
    }
  });

  // ── Combo picker: live search ──
  document.getElementById('combo-picker-search').addEventListener('input', e => {
    comboPickerApplySearch(e.target.value);
  });
  // Prevent outside-click handler from firing while typing inside picker
  document.getElementById('combo-picker-search').addEventListener('click', e => e.stopPropagation());

  // ── Combo picker: Select All ──
  document.getElementById('combo-pick-all').addEventListener('click', () => {
    document.querySelectorAll('#combo-picker-list .combo-picker-item').forEach(item => {
      const cb = item.querySelector('input[type="checkbox"]');
      cb.checked = true;
      item.classList.add('checked');
      reactionsSelectedCombos.add(cb.value);
    });
    updateComboPickerLabel();
    applyReactionsFilter();
  });

  // ── Combo picker: Clear All ──
  document.getElementById('combo-pick-none').addEventListener('click', () => {
    document.querySelectorAll('#combo-picker-list .combo-picker-item').forEach(item => {
      const cb = item.querySelector('input[type="checkbox"]');
      cb.checked = false;
      item.classList.remove('checked');
    });
    reactionsSelectedCombos.clear();
    updateComboPickerLabel();
    applyReactionsFilter();
  });

  // ── Close pickers on outside click ──
  document.addEventListener('click', e => {
    if (!e.target.closest('#combo-picker')) {
      document.getElementById('combo-picker-dropdown').classList.add('hidden');
      document.getElementById('combo-picker-btn').classList.remove('open');
    }
    if (!e.target.closest('#joint-picker')) {
      document.getElementById('joint-picker-dropdown').classList.add('hidden');
      document.getElementById('joint-picker-btn').classList.remove('open');
    }
    if (!e.target.closest('#drift-picker')) {
      document.getElementById('drift-picker-dropdown').classList.add('hidden');
      document.getElementById('drift-picker-btn').classList.remove('open');
    }
    if (!e.target.closest('.lc-name-input') && !e.target.closest('#lc-ac-popup')) lcHideAc();
  });
});

// ── Generic fetch helper (auto-attaches auth token) ──
async function apiCall(endpoint, method = 'GET') {
  const res = await authFetch(endpoint, { method });
  if (!res.ok) {
    if (res.status === 401) { localStorage.removeItem(AUTH_KEY); showAuthOverlay(); return; }
    const err = await res.json().catch(() => ({}));
    if (res.status === 402) {
      const d = typeof err.detail === 'object' ? err.detail : {};
      showGraceModal(d.days || '?', d.message || 'License verification required.');
      throw new Error('grace_expired');
    }
    if (res.status === 403) {
      const d = typeof err.detail === 'object' ? err.detail : {};
      const feat = d.required ? d.required.charAt(0).toUpperCase() + d.required.slice(1) + ' feature' : 'This feature';
      showUpgradeModal(feat);
      throw new Error('plan_required');
    }
    throw new Error(typeof err.detail === 'string' ? err.detail : `HTTP ${res.status}`);
  }
  return res.json();
}

// ── Toast ──
function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 4000);
}

// ================================================================
//  1 · STATUS CHECK
// ================================================================
async function checkStatus() {
  try {
    const data = await apiCall('/api/status');
    const pill = document.getElementById('conn-pill');
    const dot  = document.getElementById('conn-dot');
    const text = document.getElementById('connection-status');
    if (data.connected) {
      pill.classList.add('connected');
      dot.classList.add('connected');
      text.textContent = 'Connected to ETABS';
      isConnected = true;
      showToast('Successfully linked to active ETABS.');
      // Auto-load joint sources if the joint panel is currently visible
      const jointPanel = document.getElementById('joint-panel');
      if (jointPanel && jointPanel.classList.contains('active') && !jointAllCombos.length) {
        jointLoadSources();
      }
    } else {
      pill.classList.remove('connected');
      dot.classList.remove('connected');
      text.textContent = 'Disconnected';
      isConnected = false;
    }
  } catch (e) {
    showToast('Cannot reach server: ' + e.message);
  }
}

// ================================================================
//  2 · STORY DRIFTS  (multi-case, height vs drift chart)
// ================================================================

// Colour palette for drift lines (skip red — reserved for limit line)
const DRIFT_COLORS = [
  '#3b82f6', '#f59e0b', '#10b981', '#8b5cf6',
  '#f97316', '#06b6d4', '#84cc16', '#ec4899',
  '#6366f1', '#14b8a6', '#a855f7', '#fb923c',
];

async function driftGetSources() {
  const btn = document.getElementById('btn-get-drift-sources');
  btn.textContent = 'Loading…';
  btn.disabled = true;
  try {
    const [comboData, caseData] = await Promise.all([
      apiCall('/api/load-combinations'),
      apiCall('/api/load-cases'),
    ]);
    driftCombos = comboData.combinations || [];
    driftCases  = caseData.cases        || [];
    driftSelected.clear();
    driftPickerPopulate();
    showToast(`Loaded ${driftCombos.length} combos + ${driftCases.length} cases.`);
  } catch (e) {
    showToast('Error: ' + e.message);
  } finally {
    btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 16 16" fill="none"
      stroke="currentColor" stroke-width="1.8" stroke-linecap="round">
      <circle cx="8" cy="8" r="6"/>
      <line x1="8" y1="5" x2="8" y2="11"/>
      <line x1="5" y1="8" x2="11" y2="8"/></svg> Get Sources`;
    btn.disabled = false;
  }
}

// Build the picker list from driftCombos + driftCases
function driftPickerPopulate() {
  const list = document.getElementById('drift-picker-list');
  list.innerHTML = '';

  if (!driftCombos.length && !driftCases.length) {
    list.innerHTML = '<div class="combo-picker-empty">No sources found</div>';
    driftUpdatePickerLabel();
    return;
  }

  function renderGroup(names, dtype, title) {
    if (!names.length) return;
    const grp = document.createElement('div');
    grp.className = 'combo-picker-group';
    grp.dataset.groupDtype = dtype;

    const hdr = document.createElement('div');
    hdr.className = 'combo-picker-group-label';
    hdr.textContent = title;
    grp.appendChild(hdr);

    names.forEach(name => {
      const lbl = document.createElement('label');
      lbl.className = 'combo-picker-item' + (driftSelected.has(name) ? ' checked' : '');
      lbl.dataset.name  = name;
      lbl.dataset.dtype = dtype;

      const cb = document.createElement('input');
      cb.type    = 'checkbox';
      cb.checked = driftSelected.has(name);
      cb.addEventListener('change', () => {
        if (cb.checked) {
          driftSelected.set(name, dtype);
          lbl.classList.add('checked');
        } else {
          driftSelected.delete(name);
          lbl.classList.remove('checked');
        }
        driftUpdatePickerLabel();
      });

      const nameSpan = document.createElement('span');
      nameSpan.className = 'combo-picker-name';
      nameSpan.textContent = name;

      const badge = document.createElement('span');
      badge.className = `list-type-badge ${dtype === 'combo' ? 'badge-combo' : 'badge-case'}`;
      badge.textContent = dtype === 'combo' ? 'C' : 'LC';

      lbl.appendChild(cb);
      lbl.appendChild(nameSpan);
      lbl.appendChild(badge);
      grp.appendChild(lbl);
    });

    list.appendChild(grp);
  }

  renderGroup(driftCombos, 'combo', 'COMBINATIONS');
  renderGroup(driftCases,  'case',  'LOAD CASES');
  driftPickerApplyFilter('');
  driftUpdatePickerLabel();
}

// Filter visible picker items by search query and/or type toggle
function driftPickerApplyFilter(query) {
  const q = (query || '').toLowerCase();
  document.querySelectorAll('#drift-picker-list .combo-picker-item').forEach(item => {
    const name  = (item.dataset.name  || '').toLowerCase();
    const dtype = item.dataset.dtype  || '';
    const matchQuery = !q || name.includes(q);
    const matchType  = driftSourceFilter === 'all' || driftSourceFilter === dtype;
    item.style.display = (matchQuery && matchType) ? '' : 'none';
  });
  // Hide group header if all its items are hidden
  document.querySelectorAll('#drift-picker-list .combo-picker-group').forEach(grp => {
    const visible = [...grp.querySelectorAll('.combo-picker-item')].some(i => i.style.display !== 'none');
    grp.style.display = visible ? '' : 'none';
  });
}

// Update the picker button label to show selection count
function driftUpdatePickerLabel() {
  const count = driftSelected.size;
  const total = driftCombos.length + driftCases.length;
  const label = document.getElementById('drift-picker-label');
  if (!total)          label.textContent = 'Get Sources First';
  else if (!count)     label.textContent = 'No Sources Selected';
  else if (count === total) label.textContent = `All ${count} Selected`;
  else                 label.textContent = `${count} of ${total} Selected`;
}

async function driftExtract() {
  if (driftSelected.size === 0) {
    showToast('Select at least one load case / combination first.');
    return;
  }
  const btn = document.getElementById('btn-extract-drift');
  btn.textContent = 'Extracting…';
  btn.disabled = true;

  const allowable  = parseFloat(document.getElementById('drift-allowable').value)  || 0.002;
  const multiplier = parseFloat(document.getElementById('drift-multiplier').value) || 1;

  // Split selected items by type
  const selectedCombos = [];
  const selectedCases  = [];
  driftSelected.forEach((type, name) => {
    if (type === 'combo') selectedCombos.push(name);
    else                  selectedCases.push(name);
  });

  try {
    // Fire both extractions in parallel (only if there are items of that type)
    const requests = [];
    if (selectedCombos.length) {
      requests.push(authFetch('/api/results/drifts-selected', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ names: selectedCombos, load_type: 'combo' }),
      }).then(r => r.ok ? r.json() : r.json().then(e => Promise.reject(new Error(e.detail || `HTTP ${r.status}`)))));
    }
    if (selectedCases.length) {
      requests.push(authFetch('/api/results/drifts-selected', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ names: selectedCases, load_type: 'case' }),
      }).then(r => r.ok ? r.json() : r.json().then(e => Promise.reject(new Error(e.detail || `HTTP ${r.status}`)))));
    }

    const results = await Promise.all(requests);
    const allData = results.flatMap(r => r.data || []);
    driftLastData = allData;
    driftRenderTable(allData, allowable, multiplier);
    driftRenderChart(allData, allowable, multiplier);
    showToast(`Extracted ${driftSelected.size} series (${selectedCombos.length} combo, ${selectedCases.length} case).`);
  } catch (e) {
    showToast('Error: ' + e.message);
  } finally {
    btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 15 15" fill="none"
      stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <polygon points="3,2 13,7.5 3,13" fill="currentColor" stroke="none"/></svg> Extract`;
    btn.disabled = false;
  }
}

function driftRenderTable(rawData, allowable, multiplier) {
  const emptyEl = document.getElementById('drift-table-empty');
  const wrapEl  = document.getElementById('drift-table-wrap');
  const tbody   = document.getElementById('drift-table-body');

  if (!rawData.length) {
    emptyEl.style.display = '';
    wrapEl.classList.add('hidden');
    return;
  }

  // Sort: elevation descending (top story first), then by case name
  const sorted = [...rawData].sort((a, b) =>
    b.elevation - a.elevation || a.case.localeCompare(b.case)
  );

  tbody.innerHTML = '';
  sorted.forEach(row => {
    const driftAbs  = Math.abs(row.drift);
    const driftMult = driftAbs * multiplier;
    const pass      = driftMult <= allowable;
    const dir       = row.dir || row.label || '—';
    const exceedPct = pass ? '' : ` (${(((driftMult - allowable) / allowable) * 100).toFixed(1)}% over)`;

    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${row.story}</td>
      <td>${row.case}</td>
      <td>${dir}</td>
      <td>${(+row.elevation).toFixed(2)}</td>
      <td class="${driftMult > allowable ? 'val-neg' : ''}">${driftAbs.toFixed(5)}</td>
      <td class="${driftMult > allowable ? 'val-neg' : ''}">${driftMult.toFixed(5)}</td>
      <td>
        <span class="${pass ? 'badge-ok' : 'badge-warn'}">
          ${pass ? '✓ Pass' : `✗ Fail${exceedPct}`}
        </span>
      </td>`;
    tbody.appendChild(tr);
  });

  emptyEl.style.display = 'none';
  wrapEl.classList.remove('hidden');
}

function driftRenderChart(rawData, allowable, multiplier) {
  // ── Group rows by case name ──
  const byCase = {};
  rawData.forEach(row => {
    if (!byCase[row.case]) byCase[row.case] = [];
    byCase[row.case].push(row);
  });

  const allElevations = rawData.map(r => r.elevation);
  const minElev = Math.min(...allElevations);
  const maxElev = Math.max(...allElevations);

  const traces = [];
  let colorIdx = 0;

  Object.entries(byCase).forEach(([caseName, rows]) => {
    // Collapse X + Y: keep max |drift| per story
    const byStory = {};
    rows.forEach(r => {
      if (!byStory[r.story] || Math.abs(r.drift) > Math.abs(byStory[r.story].drift)) {
        byStory[r.story] = r;
      }
    });

    const sorted = Object.values(byStory).sort((a, b) => a.elevation - b.elevation);
    const color  = DRIFT_COLORS[colorIdx % DRIFT_COLORS.length];

    traces.push({
      x:    sorted.map(r => Math.abs(r.drift) * multiplier),
      y:    sorted.map(r => r.elevation),
      mode: 'lines+markers',
      type: 'scatter',
      name: caseName,
      line:   { color, width: 2 },
      marker: { color, size: 6, symbol: 'circle' },
      hovertemplate: `<b>${caseName}</b><br>Drift: %{x:.5f}<br>Elev: %{y:.1f} m<extra></extra>`,
    });
    colorIdx++;
  });

  // ── Allowable vertical line ──
  traces.push({
    x:    [allowable, allowable],
    y:    [minElev, maxElev],
    mode: 'lines',
    type: 'scatter',
    name: 'Allowable Limit',
    line: { color: '#ef4444', width: 2.5, dash: 'dash' },
    hovertemplate: `Allowable: ${allowable}<extra></extra>`,
  });

  const layout = {
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor:  'rgba(0,0,0,0)',
    margin: { l: 64, r: 24, t: 24, b: 64 },
    font:   { family: 'Inter, sans-serif', color: '#64748b', size: 11 },
    xaxis: {
      title:       { text: 'Drift Ratio', font: { size: 12, color: '#64748b' }, standoff: 8 },
      gridcolor:   'rgba(148,163,184,0.12)',
      zerolinecolor: 'rgba(148,163,184,0.2)',
      color:       '#64748b',
      tickformat:  '.4f',
    },
    yaxis: {
      title:     { text: 'Elevation (m)', font: { size: 12, color: '#64748b' }, standoff: 8 },
      gridcolor: 'rgba(148,163,184,0.12)',
      color:     '#64748b',
    },
    legend: {
      bgcolor:     'rgba(0,0,0,0)',
      borderwidth: 0,
      font:        { size: 11, color: '#64748b' },
      orientation: 'h',
      y:           -0.14,
    },
    hoverlabel: {
      bgcolor:    'rgba(8,18,34,0.92)',
      bordercolor: 'rgba(59,130,246,0.3)',
      font:       { family: 'Inter, sans-serif', size: 12, color: '#f1f5f9' },
    },
    hovermode: 'closest',
  };

  const config = { responsive: true, displayModeBar: true, displaylogo: false,
    modeBarButtonsToRemove: ['lasso2d', 'select2d', 'toImage'] };

  // ── Show chart div, hide empty state ──
  const empty = document.getElementById('drift-chart-empty');
  const div   = document.getElementById('driftsChart');
  empty.style.display = 'none';
  div.style.display   = 'block';
  driftsChartHasData  = true;

  Plotly.react('driftsChart', traces, layout, config);
}

// ================================================================
//  3 · TORSIONAL CHECK
// ================================================================
async function getTorsion() {
  const btn = document.getElementById('btn-get-torsion');
  btn.textContent = 'Checking…';
  btn.disabled = true;
  try {
    const res = await apiCall('/api/results/torsional-irregularity');
    if (res.status === 'success') {
      renderTorsionResults(res.data);
      showToast('Torsional checks complete.');
    }
  } catch (e) { showToast(e.message); }
  finally {
    btn.innerHTML = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="8" cy="8" r="6"/><polyline points="8,5 8,8 10.5,10"/></svg> Run Checks';
    btn.disabled = false;
  }
}

function renderTorsionResults(data) {
  const details   = data.details || [];
  const summaryEl = document.getElementById('torsion-summary');
  const gridEl    = document.getElementById('torsion-results');

  const total    = details.length;
  const irregular = details.filter(d => d.ratio > 1.2).length;
  const maxRatio  = details.reduce((m, d) => Math.max(m, d.ratio), 0);
  const isIrr     = data.isIrregular;

  // Summary bar
  summaryEl.classList.remove('hidden');
  summaryEl.innerHTML = `
    <div class="summary-stat">
      <div class="stat-label">Total Checks</div>
      <div class="stat-value" style="color:var(--blue-light)">${total}</div>
    </div>
    <div class="summary-stat">
      <div class="stat-label">Irregular Stories</div>
      <div class="stat-value" style="color:${irregular > 0 ? 'var(--red)' : 'var(--green)'}">${irregular}</div>
    </div>
    <div class="summary-stat">
      <div class="stat-label">Max Ratio</div>
      <div class="stat-value" style="color:${maxRatio > 1.2 ? 'var(--red)' : 'var(--green)'}">${maxRatio.toFixed(2)}</div>
    </div>
    <div class="summary-stat">
      <div class="stat-label">Overall Status</div>
      <div class="stat-value" style="color:${isIrr ? 'var(--red)' : 'var(--green)'};font-size:1rem;font-weight:800;letter-spacing:-0.5px">
        ${isIrr ? '⚠ IRREGULAR' : '✓ REGULAR'}
      </div>
    </div>`;

  // Result cards
  gridEl.innerHTML = '';
  if (!details.length) {
    gridEl.innerHTML = '<div class="empty-placeholder"><p>No results returned from analysis.</p></div>';
    return;
  }
  details.forEach(item => {
    const irr = item.ratio > 1.2;
    const card = document.createElement('div');
    card.className = `result-card${irr ? ' irregular' : ''}`;
    card.innerHTML = `
      <div class="rc-label">Story / Combo</div>
      <div class="rc-story">${item.story}</div>
      <div class="rc-combo">${item.combo}</div>
      <div class="rc-ratio" style="color:${irr ? 'var(--red)' : 'var(--green)'}">${item.ratio.toFixed(3)}</div>
      <div class="rc-badge ${irr ? 'badge-warn' : 'badge-ok'}">
        ${irr ? '⚠ Irregular' : '✓ Within Limit'}
      </div>`;
    gridEl.appendChild(card);
  });
}

// ================================================================
//  4 · BASE REACTIONS
// ================================================================

// ── Combo picker helpers ──
function populateComboPickerFromList(combos) {
  reactionsAllCombos = combos;
  // Default: all selected
  reactionsSelectedCombos = new Set(combos);

  const list = document.getElementById('combo-picker-list');
  list.innerHTML = '';

  if (!combos.length) {
    list.innerHTML = '<div class="combo-picker-empty">No combinations found</div>';
    updateComboPickerLabel();
    return;
  }

  combos.forEach(name => {
    const item = document.createElement('label');
    item.className = 'combo-picker-item checked';

    const cb = document.createElement('input');
    cb.type    = 'checkbox';
    cb.value   = name;
    cb.checked = true;
    cb.addEventListener('change', () => {
      if (cb.checked) {
        reactionsSelectedCombos.add(name);
        item.classList.add('checked');
      } else {
        reactionsSelectedCombos.delete(name);
        item.classList.remove('checked');
      }
      updateComboPickerLabel();
      applyReactionsFilter();
    });

    const span = document.createElement('span');
    span.textContent = name;

    item.appendChild(cb);
    item.appendChild(span);
    list.appendChild(item);
  });

  updateComboPickerLabel();
}

function comboPickerApplySearch(query) {
  const q = query.toLowerCase().trim();
  let visibleCount = 0;
  document.querySelectorAll('#combo-picker-list .combo-picker-item').forEach(item => {
    const name = item.querySelector('span').textContent.toLowerCase();
    const match = !q || name.includes(q);
    item.style.display = match ? '' : 'none';
    if (match) visibleCount++;
  });
  // Show "no matches" hint when nothing passes the filter
  let noMatch = document.getElementById('combo-picker-no-match');
  if (visibleCount === 0 && q) {
    if (!noMatch) {
      noMatch = document.createElement('div');
      noMatch.id = 'combo-picker-no-match';
      noMatch.className = 'combo-picker-empty';
      noMatch.textContent = 'No matches';
      document.getElementById('combo-picker-list').appendChild(noMatch);
    }
    noMatch.style.display = '';
  } else if (noMatch) {
    noMatch.style.display = 'none';
  }
}

function updateComboPickerLabel() {
  const label = document.getElementById('combo-picker-label');
  const total = reactionsAllCombos.length;
  const sel   = reactionsSelectedCombos.size;
  if (total === 0 || sel === total) label.textContent = 'All Combinations';
  else if (sel === 0)               label.textContent = 'None Selected';
  else                              label.textContent = `${sel} of ${total} Selected`;
}

function reactionsGetFiltered() {
  if (reactionsSelectedCombos.size === 0)                          return [];
  if (reactionsSelectedCombos.size === reactionsAllCombos.length)  return reactionsData;
  return reactionsData.filter(r => reactionsSelectedCombos.has(r.combo));
}

function applyReactionsFilter() {
  const filtered = reactionsGetFiltered();
  renderReactionsTableDOM(filtered);
  const chartPanel = document.getElementById('reactions-chart-tab');
  if (chartPanel && chartPanel.classList.contains('active')) {
    renderReactionsChart(filtered);
  }
}

async function populateReactionCombos() {
  try {
    const res = await apiCall(`/api/results/reactions?load_type=${reactionsLoadType}`);
    if (res.status === 'success') {
      const available = reactionsLoadType === 'case'
        ? (res.available_cases || [])
        : (res.available_combos || []);
      if (available.length) populateComboPickerFromList(available);
    }
  } catch (_) { /* silent — ETABS may not be connected yet */ }
}

async function getReactions() {
  const btn = document.getElementById('btn-get-reactions');
  btn.textContent = 'Loading…';
  btn.disabled = true;
  try {
    const res = await apiCall(`/api/results/reactions?load_type=${reactionsLoadType}`);
    if (res.status === 'success') {
      reactionsData = res.data;
      const available = reactionsLoadType === 'case'
        ? (res.available_cases || [])
        : (res.available_combos || []);
      populateComboPickerFromList(available);
      applyReactionsFilter();
      const typeLabel = reactionsLoadType === 'case' ? 'load cases' : 'combinations';
      showToast(`Base reactions loaded — ${reactionsData.length} row${reactionsData.length === 1 ? '' : 's'} (${typeLabel}).`);
    }
  } catch (e) { showToast(e.message); }
  finally {
    btn.innerHTML = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><line x1="8" y1="2" x2="8" y2="11"/><polyline points="5,8 8,11 11,8"/><line x1="3" y1="14" x2="13" y2="14"/></svg> Fetch';
    btn.disabled = false;
  }
}

function renderReactionsTableDOM(data) {
  const tbody = document.getElementById('reactions-tbody');
  tbody.innerHTML = '';
  if (!data.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="td-empty"><div class="empty-placeholder">No results for selected combination(s).</div></td></tr>';
    return;
  }
  data.forEach(r => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><strong>${r.combo}</strong></td>
      <td class="${r.FX < 0 ? 'val-neg' : ''}">${fmtF(r.FX)}</td>
      <td class="${r.FY < 0 ? 'val-neg' : ''}">${fmtF(r.FY)}</td>
      <td class="val-fz">${fmtF(r.FZ)}</td>
      <td class="val-moment">${fmtF(r.MX)}</td>
      <td class="val-moment">${fmtF(r.MY)}</td>
      <td class="val-moment">${fmtF(r.MZ)}</td>`;
    tbody.appendChild(tr);
  });
}

function renderReactionsChart(data) {
  const emptyEl = document.getElementById('reactions-chart-empty');
  const div     = document.getElementById('reactionsChart');
  if (!data.length) return;

  emptyEl.style.display = 'none';
  div.style.display     = 'block';
  reactionsChartHasData = true;

  // X-axis labels = unique combo names (preserving order)
  const labels = [...new Set(data.map(r => r.combo))];

  const traces = ['FX','FY','FZ','MX','MY','MZ']
    .filter(f => reactionsActiveForces.has(f))
    .map(force => {
      const isMoment = ['MX','MY','MZ'].includes(force);
      return {
        x:    labels,
        y:    labels.map(combo => {
          const row = data.find(r => r.combo === combo);
          return row ? row[force] : 0;
        }),
        name: force,
        type: 'bar',
        marker:        { color: FORCE_COLORS[force], opacity: 0.85 },
        hovertemplate: `<b>%{x}</b><br>${force}: %{y:.1f} ${isMoment ? 'kN·m' : 'kN'}<extra></extra>`,
      };
    });

  const layout = {
    barmode:       'group',
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor:  'rgba(0,0,0,0)',
    margin: { l: 64, r: 24, t: 24, b: 80 },
    font:   { family: 'Inter, sans-serif', color: '#64748b', size: 11 },
    xaxis: {
      gridcolor: 'rgba(148,163,184,0.08)',
      color:     '#64748b',
      tickangle: -30,
    },
    yaxis: {
      title:       { text: 'kN / kN·m', font: { size: 12, color: '#64748b' }, standoff: 8 },
      gridcolor:   'rgba(148,163,184,0.12)',
      zerolinecolor: 'rgba(148,163,184,0.3)',
      color:       '#64748b',
    },
    legend: {
      bgcolor:     'rgba(0,0,0,0)',
      borderwidth: 0,
      font:        { size: 11, color: '#64748b' },
      orientation: 'h',
      y:           -0.2,
    },
    hoverlabel: {
      bgcolor:     'rgba(8,18,34,0.92)',
      bordercolor: 'rgba(59,130,246,0.3)',
      font:        { family: 'Inter, sans-serif', size: 12, color: '#f1f5f9' },
    },
  };

  const config = { responsive: true, displayModeBar: true, displaylogo: false,
    modeBarButtonsToRemove: ['lasso2d', 'select2d', 'toImage'] };

  Plotly.react('reactionsChart', traces, layout, config);
}

// ================================================================
//  5 · JOINT / SPRING REACTIONS
// ================================================================

// ── Picker helpers ──
function populateJointPickerFromList(combos) {
  jointAllCombos = combos;
  jointSelectedCombos = new Set();   // start EMPTY — user must pick explicitly

  const list = document.getElementById('joint-picker-list');
  list.innerHTML = '';

  if (!combos.length) {
    list.innerHTML = '<div class="combo-picker-empty">No items found</div>';
    updateJointPickerLabel();
    return;
  }

  combos.forEach(name => {
    const item = document.createElement('label');
    item.className = 'combo-picker-item';   // NOT pre-checked

    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.value = name; cb.checked = false;
    cb.addEventListener('change', () => {
      if (cb.checked) {
        jointSelectedCombos.add(name);
        item.classList.add('checked');
      } else {
        jointSelectedCombos.delete(name);
        item.classList.remove('checked');
      }
      updateJointPickerLabel();
      applyJointFilter();
    });

    const span = document.createElement('span');
    span.textContent = name;

    item.appendChild(cb); item.appendChild(span);
    list.appendChild(item);
  });

  updateJointPickerLabel();
}

function jointPickerApplySearch(query) {
  const q = query.toLowerCase().trim();
  let count = 0;
  document.querySelectorAll('#joint-picker-list .combo-picker-item').forEach(item => {
    const match = !q || item.querySelector('span').textContent.toLowerCase().includes(q);
    item.style.display = match ? '' : 'none';
    if (match) count++;
  });
  let noMatch = document.getElementById('joint-picker-no-match');
  if (count === 0 && q) {
    if (!noMatch) {
      noMatch = document.createElement('div');
      noMatch.id = 'joint-picker-no-match';
      noMatch.className = 'combo-picker-empty';
      noMatch.textContent = 'No matches';
      document.getElementById('joint-picker-list').appendChild(noMatch);
    }
    noMatch.style.display = '';
  } else if (noMatch) {
    noMatch.style.display = 'none';
  }
}

function updateJointPickerLabel() {
  const label = document.getElementById('joint-picker-label');
  const total = jointAllCombos.length;
  const sel   = jointSelectedCombos.size;
  const typeWord = jointLoadType === 'case' ? 'Load Cases' : 'Combinations';
  if (total === 0)   label.textContent = `Select ${typeWord}`;
  else if (sel === 0) label.textContent = `— Pick a ${jointLoadType === 'case' ? 'Case' : 'Combo'} to Fetch —`;
  else if (sel === total) label.textContent = `All ${total} ${typeWord}`;
  else                    label.textContent = `${sel} of ${total} Selected`;
}

function jointGetFiltered() {
  if (jointSelectedCombos.size === 0)                        return [];
  if (jointSelectedCombos.size === jointAllCombos.length)    return jointData;
  return jointData.filter(r => jointSelectedCombos.has(r.combo));
}

function applyJointFilter() {
  const filtered = jointGetFiltered();
  renderJointTable(filtered);
  // Refresh bubble plot if it's the active tab and has a combo selected
  const bubbleTab = document.getElementById('joint-bubble-tab');
  if (bubbleTab && bubbleTab.classList.contains('active') && jointBubbleCombo) {
    renderJointBubblePlot(filtered, jointActiveForce, jointBubbleCombo);
  }
}

// ── Populate bubble-plot combo/case dropdown ──
function populateJointBubbleComboSelect(data) {
  const sel   = document.getElementById('joint-bubble-combo');
  const combos = [...new Set(data.map(r => r.combo))];
  sel.innerHTML = '';
  combos.forEach((c, i) => {
    const opt = document.createElement('option');
    opt.value = c; opt.textContent = c;
    if (i === 0) opt.selected = true;
    sel.appendChild(opt);
  });
  jointBubbleCombo = combos[0] || '';
}

// ── Fetch ──
// ── Phase 1: load just the list of available combos/cases into the picker ──
async function jointLoadSources() {
  const list  = document.getElementById('joint-picker-list');
  const label = document.getElementById('joint-picker-label');
  list.innerHTML = `<div class="combo-picker-empty">Loading…</div>`;
  try {
    const url = jointLoadType === 'case' ? '/api/load-cases' : '/api/load-combinations';
    const res = await apiCall(url);
    const items = jointLoadType === 'case'
      ? (res.cases         || [])
      : (res.combinations  || []);
    if (!items.length) {
      list.innerHTML = `<div class="combo-picker-empty">No ${jointLoadType === 'case' ? 'load cases' : 'combinations'} found</div>`;
      return;
    }
    populateJointPickerFromList(items);
    const typeWord = jointLoadType === 'case' ? 'load cases' : 'combinations';
    label.textContent = `All ${items.length} ${typeWord}`;
  } catch (e) {
    list.innerHTML = `<div class="combo-picker-empty">Failed to load — check connection</div>`;
  }
}

// ── Phase 2: fetch reactions only for selected combos/cases ──
async function getJointReactions() {
  const btn = document.getElementById('btn-get-joint-reactions');

  // If sources haven't been loaded yet, load them now and return (user should
  // review the selection before fetching potentially large data)
  if (!jointAllCombos.length) {
    await jointLoadSources();
    showToast('Sources loaded — review your selection, then click Fetch again.');
    return;
  }
  if (!jointSelectedCombos.size) {
    showToast('Select at least one combination / case first.');
    return;
  }

  btn.textContent = 'Loading…';
  btn.disabled = true;
  try {
    // Always send the explicit selection — never fetch all at once
    const url = `/api/results/joint-reactions?load_type=${jointLoadType}`
              + `&names=${[...jointSelectedCombos].map(encodeURIComponent).join(',')}`;

    const res = await apiCall(url);
    if (res.status === 'success') {
      jointData = res.data;
      populateJointBubbleComboSelect(jointData);
      renderJointTable(jointData);
      const sel = [...jointSelectedCombos].length;
      const typeLabel = jointLoadType === 'case' ? 'load cases' : 'combinations';
      showToast(`Joint reactions loaded — ${jointData.length} rows across ${sel} ${typeLabel}.`);
    }
  } catch (e) { showToast(e.message); }
  finally {
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><line x1="8" y1="2" x2="8" y2="11"/><polyline points="5,8 8,11 11,8"/><line x1="3" y1="14" x2="13" y2="14"/></svg> Fetch';
    btn.disabled = false;
  }
}

// ── Table rendering ──
/** Smart force formatter: shows enough decimals so small values aren't hidden as 0.0 */
function fmtF(v) {
  const a = Math.abs(v);
  if (a === 0)   return '0.0';
  if (a >= 100)  return v.toFixed(1);
  if (a >= 1)    return v.toFixed(2);
  return v.toFixed(3);  // e.g. 0.150, -0.025
}

function renderJointTable(data) {
  const tbody = document.getElementById('joint-tbody');
  tbody.innerHTML = '';
  if (!data.length) {
    tbody.innerHTML = '<tr><td colspan="12" class="td-empty"><div class="empty-placeholder">No results. Select a combination and click Fetch.</div></td></tr>';
    return;
  }
  data.forEach(r => {
    const tr = document.createElement('tr');
    // step_type distinguishes envelope steps (Max/Min) from linear single steps
    const stepLabel = r.step_type || '';
    const stepClass = stepLabel === 'Max' ? 'step-max'
                    : stepLabel === 'Min' ? 'step-min'
                    : 'step-other';
    tr.innerHTML = `
      <td><strong>${r.joint}</strong></td>
      <td>${r.combo}</td>
      <td class="val-step ${stepClass}">${stepLabel}</td>
      <td class="val-coord">${r.x.toFixed(3)}</td>
      <td class="val-coord">${r.y.toFixed(3)}</td>
      <td class="val-coord">${r.z.toFixed(3)}</td>
      <td class="${r.FX < 0 ? 'val-neg' : ''}">${fmtF(r.FX)}</td>
      <td class="${r.FY < 0 ? 'val-neg' : ''}">${fmtF(r.FY)}</td>
      <td class="val-fz">${fmtF(r.FZ)}</td>
      <td class="val-moment">${fmtF(r.MX)}</td>
      <td class="val-moment">${fmtF(r.MY)}</td>
      <td class="val-moment">${fmtF(r.MZ)}</td>`;
    tbody.appendChild(tr);
  });
}

// ── Bubble plot rendering ──
function renderJointBubblePlot(allData, force, comboName) {
  const emptyEl = document.getElementById('joint-bubble-empty');
  const div     = document.getElementById('jointBubbleChart');

  if (!allData.length || !comboName) {
    emptyEl.style.display = ''; div.style.display = 'none'; return;
  }

  // Filter to one combo/case
  const rawData = allData.filter(r => r.combo === comboName);
  if (!rawData.length) {
    emptyEl.style.display = ''; div.style.display = 'none'; return;
  }

  // Deduplicate by joint for the bubble plot.
  // Envelope combos return multiple steps (Max, Min, …) per joint at the same XY
  // position — stacking bubbles makes the chart unreadable.
  // Priority: "Max" > "Min" > first row encountered.
  const STEP_PRIORITY = { 'Max': 0, 'Min': 1 };
  const seen = new Map();  // joint → best row
  rawData.forEach(r => {
    const cur  = seen.get(r.joint);
    const rPri = STEP_PRIORITY[r.step_type] ?? 2;
    const cPri = cur ? (STEP_PRIORITY[cur.step_type] ?? 2) : Infinity;
    if (!cur || rPri < cPri) seen.set(r.joint, r);
  });
  const data     = [...seen.values()];
  const stepTypes = [...new Set(rawData.map(r => r.step_type))];
  const stepNote  = stepTypes.length > 1
    ? ` — Bubble shows "${data[0]?.step_type || 'Max'}" step (${stepTypes.join(', ')} available in table)`
    : '';

  const values   = data.map(r => r[force]);
  const absVals  = values.map(v => Math.abs(v));
  const maxAbs   = Math.max(...absVals, 0.001);

  // Bubble size: scale is applied via sizeref — smaller sizeref = bigger bubbles
  // jointBubbleScale > 1 enlarges, < 1 shrinks
  const sizeref = maxAbs / (55 * jointBubbleScale);

  // ── Active colorscale (set by palette selector)
  const colorscale = jointColorPalette;

  const isMoment  = ['MX','MY','MZ'].includes(force);
  const unitLabel = isMoment ? 'kN·m' : 'kN';

  // Effective color range: user-set values, or auto (data min/max)
  const dataMin = Math.min(...values);
  const dataMax = Math.max(...values);
  const effCmin = jointColorMin !== null ? jointColorMin : dataMin;
  const effCmax = jointColorMax !== null ? jointColorMax : dataMax;

  // Per-palette text contrast rules.
  // Sequential palettes: bright zone is at the HIGH end (norm > threshold → dark text).
  // Diverging/rainbow palettes: bright zone is in the MIDDLE BAND → dark text there.
  const range = effCmax !== effCmin ? effCmax - effCmin : 1;
  const textColors = values.map(v => {
    const norm = Math.max(0, Math.min(1, (v - effCmin) / range));
    let dark = false;
    switch (jointColorPalette) {
      // Sequential — bright at the top
      case 'Plasma':    dark = norm > 0.82; break;
      case 'Viridis':   dark = norm > 0.72; break;
      case 'Inferno':   dark = false;        break;  // tip is pale-yellow, but text small enough
      case 'Magma':     dark = false;        break;
      case 'Hot':       dark = norm > 0.68; break;
      case 'Blackbody': dark = norm > 0.80; break;
      case 'Electric':  dark = false;        break;
      // Diverging / rainbow — bright in middle or top
      case 'Jet':       dark = norm > 0.38 && norm < 0.63; break;  // cyan-yellow band
      case 'RdYlBu':    dark = norm > 0.28 && norm < 0.72; break;  // yellow middle
      case 'RdBu':      dark = norm > 0.38 && norm < 0.62; break;  // white middle
      case 'Bluered':   dark = false;        break;
      case 'Rainbow':   dark = norm > 0.30 && norm < 0.55; break;  // yellow-green band
      default:          dark = false;
    }
    return dark ? '#1e293b' : '#ffffff';
  });

  const trace = {
    x:    data.map(r => r.x),
    y:    data.map(r => r.y),
    mode: 'markers+text',
    type: 'scatter',
    text: values.map(v => {
      const a = Math.abs(v);
      if (a === 0)    return '0';
      if (a >= 1000)  return String(Math.round(v));
      if (a >= 100)   return v.toFixed(1);
      if (a >= 1)     return v.toFixed(2);
      return v.toFixed(3);   // small decimals like 0.15, -0.025
    }),
    textposition: 'middle center',
    textfont: {
      size:   jointTextSize,   // controlled by Text slider
      color:  textColors,
      family: 'Inter, sans-serif',
    },
    customdata: data.map(r => [r.joint, r.z]),
    hovertemplate:
      '<b>Joint: %{customdata[0]}</b><br>' +
      'X: %{x:.3f} m   Y: %{y:.3f} m   Z: %{customdata[1]:.3f} m<br>' +
      `${force}: <b>%{marker.color:.2f} ${unitLabel}</b><extra></extra>`,
    marker: {
      size:      absVals.map(v => Math.max(v, maxAbs * 0.08)),
      sizeref,
      sizemode:  'diameter',
      sizemin:   Math.max(8, Math.round(22 * jointBubbleScale)),  // scales with bubble control
      color:      values,
      colorscale,
      cmin:       effCmin,
      cmax:       effCmax,
      opacity:    1,          // fully solid bubbles
      showscale:  true,
      colorbar:  {
        title:    { text: `${force} (${unitLabel})`, font: { size: 11, color: '#64748b' }, side: 'right' },
        thickness: 14,
        len:       0.85,
        tickfont:  { size: 10, color: '#64748b' },
        outlinewidth: 0,
        bgcolor:   'rgba(0,0,0,0)',
      },
      line: { color: 'rgba(255,255,255,0.35)', width: 1.0 },  // subtle border on Viridis bubbles
    },
  };

  const layout = {
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor:  'rgba(0,0,0,0)',
    margin: { l: 64, r: 90, t: 48, b: 64 },
    font:   { family: 'Inter, sans-serif', color: '#64748b', size: 11 },
    title:  {
      text: `<b>${force}</b> Reactions — ${comboName}${stepNote}`,
      font: { size: 13, color: '#1e293b', family: 'Inter, sans-serif' },
      x: 0.46, y: 0.98, xanchor: 'center',
    },
    xaxis: (() => {
      const xs   = data.map(r => r.x);
      const dmin = Math.min(...xs), dmax = Math.max(...xs);
      const pad  = (dmax - dmin) * 0.12 || 1;
      const xMin = jointAxisX.min !== null ? jointAxisX.min : dmin - pad;
      const xMax = jointAxisX.max !== null ? jointAxisX.max : dmax + pad;
      const hasCustom = jointAxisX.min !== null || jointAxisX.max !== null;
      return {
        title:        { text: 'X Coordinate (m)', font: { size: 12, color: '#64748b' }, standoff: 8 },
        gridcolor:    'rgba(148,163,184,0.15)',
        zerolinecolor:'rgba(148,163,184,0.4)',
        color:        '#64748b',
        scaleanchor:  hasCustom ? undefined : 'y',  // disable equal-aspect when user sets a custom range
        scaleratio:   hasCustom ? undefined : 1,
        autorange:    !hasCustom,
        ...(hasCustom ? { range: [xMin, xMax] } : {}),
      };
    })(),
    yaxis: (() => {
      const ys   = data.map(r => r.y);
      const dmin = Math.min(...ys), dmax = Math.max(...ys);
      const pad  = (dmax - dmin) * 0.12 || 1;
      const yMin = jointAxisY.min !== null ? jointAxisY.min : dmin - pad;
      const yMax = jointAxisY.max !== null ? jointAxisY.max : dmax + pad;
      const hasCustom = jointAxisY.min !== null || jointAxisY.max !== null;
      return {
        title:        { text: 'Y Coordinate (m)', font: { size: 12, color: '#64748b' }, standoff: 8 },
        gridcolor:    'rgba(148,163,184,0.15)',
        zerolinecolor:'rgba(148,163,184,0.4)',
        color:        '#64748b',
        autorange:    !hasCustom,
        ...(hasCustom ? { range: [yMin, yMax] } : {}),
      };
    })(),
    hoverlabel: {
      bgcolor:     'rgba(8,18,34,0.92)',
      bordercolor: 'rgba(59,130,246,0.3)',
      font:        { family: 'Inter, sans-serif', size: 12, color: '#f1f5f9' },
    },
    hovermode:  'closest',
    showlegend: false,
  };

  const config = { responsive: true, displayModeBar: true, displaylogo: false,
    modeBarButtonsToRemove: ['lasso2d', 'select2d', 'toImage'] };

  emptyEl.style.display = 'none';
  div.style.display     = 'block';

  Plotly.react('jointBubbleChart', [trace], layout, config);
}

// ================================================================
//  6 · LOAD COMBINATION SPREADSHEET
// ================================================================

// State
let lcColumns    = [];
let lcAllCases   = [];
let lcHiddenCols = new Set();
let lcRowId      = 0;
let lcAcIndex    = -1;
let lcAcRowEl    = null;

// ── Templates ──
const LC_TEMPLATES = [
  // ACI 318 / ASCE 7
  { name: '1.4D',          factors: { DL: 1.4 } },
  { name: '1.2D+1.6L',     factors: { DL: 1.2, LL: 1.6 } },
  { name: '1.2D+1.6Lr+L',  factors: { DL: 1.2, LLR: 1.6, LL: 1.0 } },
  { name: '1.2D+1.0W+L',   factors: { DL: 1.2, W0: 1.0,  LL: 1.0 } },
  { name: '0.9D+1.0W',     factors: { DL: 0.9, W0: 1.0 } },
  { name: '1.2D+Ex+L',     factors: { DL: 1.2, EX: 1.0,  LL: 1.0 } },
  { name: '1.2D+Ey+L',     factors: { DL: 1.2, EY: 1.0,  LL: 1.0 } },
  { name: '0.9D+Ex',       factors: { DL: 0.9, EX: 1.0 } },
  { name: '0.9D+Ey',       factors: { DL: 0.9, EY: 1.0 } },
  // S-series (NSCP / project)
  { name: 'S1',            factors: { DL: 1, SDL: 1 } },
  { name: 'S1+H',          factors: { DL: 1, SDL: 1, H: 1 } },
  { name: 'S1+H+U',        factors: { DL: 1, SDL: 1, H: 1, U: 1 } },
  { name: 'S2A',           factors: { DL: 1, SDL: 1, LL: 1, LLR: 1, MLL: 1, EX:  1 } },
  { name: 'S2B',           factors: { DL: 1, SDL: 1, LL: 1, LLR: 1, MLL: 1, EY: -1 } },
  { name: 'S2A+H',         factors: { DL: 1, SDL: 1, LL: 1, LLR: 1, MLL: 1, H: 1, EX:  1 } },
  { name: 'S2A+H+U',       factors: { DL: 1, SDL: 1, LL: 1, LLR: 1, MLL: 1, H: 1, U: 1, EX:  1 } },
  { name: 'S2B+H',         factors: { DL: 1, SDL: 1, LL: 1, LLR: 1, MLL: 1, H: 1, EY: -1 } },
  { name: 'S2B+H+U',       factors: { DL: 1, SDL: 1, LL: 1, LLR: 1, MLL: 1, H: 1, U: 1, EY: -1 } },
  { name: 'S3A',           factors: { DL: 1, SDL: 1, LL: 0.75, LLR: 0.75, MLL: 0.75, EX:  0.75 } },
  { name: 'S3B',           factors: { DL: 1, SDL: 1, LL: 0.75, LLR: 0.75, MLL: 0.75, EY: -0.75 } },
  { name: 'S3A+H',         factors: { DL: 1, SDL: 1, LL: 0.75, LLR: 0.75, MLL: 0.75, H: 1, EX:  1 } },
  { name: 'S3A+H+U',       factors: { DL: 1, SDL: 1, LL: 0.75, LLR: 0.75, MLL: 0.75, H: 1, U: 0.75, EX:  1 } },
  { name: 'S3B+H',         factors: { DL: 1, SDL: 1, LL: 0.75, LLR: 0.75, MLL: 0.75, H: 1, EY: -0.75 } },
  { name: 'S3B+H+U',       factors: { DL: 1, SDL: 1, LL: 0.75, LLR: 0.75, MLL: 0.75, H: 1, U: 0.75, EY: -0.75 } },
  // S4-WT Wind Tunnel (22 directions)
  ...Array.from({ length: 22 }, (_, i) => ({
    name: `S4-WT${i + 1}`,
    factors: { DL: 1, SDL: 1, [`S-WT${i + 1}`]: 1 }
  }))
];

// ── Header rebuild ──
function lcBuildHeader() {
  const tr = document.getElementById('lc-thead-row');
  while (tr.children.length > 2) tr.removeChild(tr.lastChild); // keep # and Names

  lcColumns.forEach(col => {
    if (lcHiddenCols.has(col)) return;
    const th = document.createElement('th');
    th.className = 'lc-th-col';
    th.textContent = col;
    th.title = col;
    tr.appendChild(th);
  });

  const thDel = document.createElement('th');
  thDel.className = 'lc-th-del';
  tr.appendChild(thDel);

  document.querySelectorAll('#lc-tbody tr:not(#lc-empty-row)').forEach(row => lcRebuildRow(row));
  lcUpdateEmptyState();
}

function lcRebuildRow(rowEl) {
  const nameVal = rowEl.querySelector('.lc-name-input')?.value || '';
  const saved   = {};
  rowEl.querySelectorAll('.lc-factor-input').forEach(i => { saved[i.dataset.col] = i.value; });

  rowEl.innerHTML = '';
  const rn = parseInt(rowEl.dataset.rowId);
  rowEl.appendChild(lcMakeRownumCell(rn));
  rowEl.appendChild(lcMakeNameCell(nameVal, rowEl));
  lcColumns.forEach(col => {
    if (!lcHiddenCols.has(col)) rowEl.appendChild(lcMakeFactorCell(col, saved[col] ?? ''));
  });
  rowEl.appendChild(lcMakeDeleteCell(rowEl));
}

// ── Cell factories ──
function lcMakeRownumCell(num) {
  const td = document.createElement('td');
  td.className = 'lc-td-rownum';
  td.textContent = num;
  return td;
}

function lcMakeNameCell(value, rowEl) {
  const td  = document.createElement('td');
  td.className = 'lc-td-name';
  const inp = document.createElement('input');
  inp.type        = 'text';
  inp.className   = 'lc-name-input';
  inp.value       = value;
  inp.placeholder = 'Combo name…';
  inp.autocomplete = 'off';
  inp.addEventListener('input',   () => lcShowAc(inp, rowEl));
  inp.addEventListener('focus',   () => { if (inp.value) lcShowAc(inp, rowEl); });
  inp.addEventListener('keydown', e  => lcHandleAcKey(e, inp, rowEl));
  inp.addEventListener('blur',    () => setTimeout(lcHideAc, 160));
  td.appendChild(inp);
  return td;
}

function lcMakeFactorCell(col, value) {
  const td  = document.createElement('td');
  td.className = 'lc-td-factor';
  const inp = document.createElement('input');
  inp.type      = 'number';
  inp.className = 'lc-factor-input';
  inp.dataset.col = col;
  inp.value     = value;
  inp.step      = 'any';
  inp.addEventListener('focus', () => inp.select());
  inp.addEventListener('input', () => lcStyleFactor(inp));
  lcStyleFactor(inp);
  td.appendChild(inp);
  return td;
}

function lcStyleFactor(inp) {
  const v = parseFloat(inp.value);
  inp.classList.toggle('has-value',   !isNaN(v) && v !== 0);
  inp.classList.toggle('is-negative', !isNaN(v) && v < 0);
}

function lcMakeDeleteCell(rowEl) {
  const td  = document.createElement('td');
  td.className = 'lc-td-del';

  const copyBtn = document.createElement('button');
  copyBtn.className = 'lc-copy-btn';
  copyBtn.title     = 'Duplicate row';
  copyBtn.innerHTML = `<svg width="12" height="12" viewBox="0 0 16 16" fill="none"
    stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
    <rect x="5" y="5" width="9" height="9" rx="1.5"/>
    <path d="M3 11V2.5A1.5 1.5 0 0 1 4.5 1H13"/>
  </svg>`;
  copyBtn.addEventListener('click', () => lcCopyRow(rowEl));
  td.appendChild(copyBtn);

  const delBtn = document.createElement('button');
  delBtn.className = 'lc-del-btn';
  delBtn.innerHTML = '&times;';
  delBtn.title     = 'Delete row';
  delBtn.addEventListener('click', () => { rowEl.remove(); lcUpdateRowCount(); lcUpdateEmptyState(); });
  td.appendChild(delBtn);

  return td;
}

// ── Copy row ──
function lcCopyRow(rowEl) {
  const name    = rowEl.querySelector('.lc-name-input')?.value || '';
  const factors = {};
  rowEl.querySelectorAll('.lc-factor-input').forEach(inp => { factors[inp.dataset.col] = inp.value; });
  lcAddRow({ name: name + ' (copy)', factors }, rowEl);
}

// ── Add row ──
function lcAddRow(template = null, afterEl = null) {
  const emptyRow = document.getElementById('lc-empty-row');
  if (emptyRow) emptyRow.remove();

  const tbody = document.getElementById('lc-tbody');
  const tr    = document.createElement('tr');
  const id    = ++lcRowId;
  tr.dataset.rowId = id;

  const tf = template ? template.factors : {};
  tr.appendChild(lcMakeRownumCell(id));
  tr.appendChild(lcMakeNameCell(template ? template.name : '', tr));
  lcColumns.forEach(col => {
    if (!lcHiddenCols.has(col)) tr.appendChild(lcMakeFactorCell(col, tf[col] !== undefined ? tf[col] : ''));
  });
  tr.appendChild(lcMakeDeleteCell(tr));

  if (afterEl) afterEl.insertAdjacentElement('afterend', tr);
  else tbody.appendChild(tr);

  if (!template) tr.querySelector('.lc-name-input').focus();
  lcUpdateRowCount();
  return tr;
}

function lcUpdateRowCount() {
  const n = document.querySelectorAll('#lc-tbody tr:not(#lc-empty-row)').length;
  document.getElementById('lc-row-count').textContent = `${n} row${n === 1 ? '' : 's'}`;
}

function lcUpdateEmptyState() {
  const rows = document.querySelectorAll('#lc-tbody tr:not(#lc-empty-row)');
  const tbody = document.getElementById('lc-tbody');
  const hasEmpty = !!document.getElementById('lc-empty-row');
  if (rows.length === 0 && !hasEmpty) {
    const tr = document.createElement('tr');
    tr.id = 'lc-empty-row';
    tr.innerHTML = `<td colspan="${2 + lcColumns.filter(c => !lcHiddenCols.has(c)).length + 1}" class="lc-empty-cell">
      <div class="lc-empty-state">
        <svg viewBox="0 0 52 52" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round">
          <rect x="4" y="8" width="44" height="36" rx="2"/>
          <line x1="4"  y1="18" x2="48" y2="18"/><line x1="4"  y1="28" x2="48" y2="28"/><line x1="4"  y1="38" x2="48" y2="38"/>
          <line x1="16" y1="8"  x2="16" y2="44"/><line x1="28" y1="8"  x2="28" y2="44"/>
        </svg>
        <p>Click <strong>+ Add Row</strong> to create a combination row.</p>
      </div></td>`;
    tbody.appendChild(tr);
  }
}

// ── Actions ──
async function lcFetchCases() {
  const btn = document.getElementById('btn-get-lc');
  btn.textContent = 'Loading…';
  try {
    const res = await apiCall('/api/load-cases');
    if (res.status === 'success') {
      lcAllCases = res.cases;
      lcColumns  = [...res.cases];
      lcHiddenCols.clear();
      lcUpdateCasesList();
      lcBuildHeader();
      showToast(`Loaded ${res.cases.length} load case(s).`);
    }
  } catch (e) { showToast(e.message); }
  finally { btn.textContent = 'Get LoadCases'; }
}

function lcClearCases() {
  lcColumns = [];
  lcAllCases = [];
  lcHiddenCols.clear();
  lcUpdateCasesList();
  document.getElementById('lc-tbody').innerHTML = '';
  lcRowId = 0;
  lcBuildHeader();
  lcUpdateRowCount();
  lcUpdateEmptyState();
  showToast('Load cases cleared.');
}

function lcUpdateCasesList() {
  const ul      = document.getElementById('lc-cases-list');
  const emptyEl = document.getElementById('lc-cases-empty');
  ul.innerHTML = '';
  lcAllCases.forEach(name => {
    const li = document.createElement('li');
    li.className   = 'lc-case-item';
    li.textContent = name;
    ul.appendChild(li);
  });
  if (emptyEl) emptyEl.style.display = lcAllCases.length ? 'none' : '';
}

function lcShowFilterModal() {
  if (!lcAllCases.length) { showToast('Click "Get LoadCases" first.'); return; }
  const list = document.getElementById('lc-filter-checks');
  list.innerHTML = '';
  lcAllCases.forEach(col => {
    const lbl = document.createElement('label');
    lbl.className = 'lc-filter-item';
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.value = col; cb.checked = !lcHiddenCols.has(col);
    lbl.appendChild(cb);
    lbl.append('\u00a0' + col);
    list.appendChild(lbl);
  });
  document.getElementById('lc-filter-modal').classList.remove('hidden');
}

function lcApplyFilter() {
  lcHiddenCols.clear();
  document.querySelectorAll('#lc-filter-checks input[type=checkbox]').forEach(cb => {
    if (!cb.checked) lcHiddenCols.add(cb.value);
  });
  lcBuildHeader();
  document.getElementById('lc-filter-modal').classList.add('hidden');
}

async function lcGenerateBatch() {
  const btn      = document.getElementById('btn-generate-batch');
  const statusEl = document.getElementById('lc-gen-status');
  btn.textContent = 'Generating…';

  const combinations = [];
  document.querySelectorAll('#lc-tbody tr:not(#lc-empty-row)').forEach(row => {
    const name = row.querySelector('.lc-name-input')?.value.trim();
    if (!name) return;
    const factors = {};
    row.querySelectorAll('.lc-factor-input').forEach(inp => {
      const v = parseFloat(inp.value);
      if (!isNaN(v) && v !== 0) factors[inp.dataset.col] = v;
    });
    combinations.push({ name, combo_type: 0, factors });
  });

  if (!combinations.length) {
    showToast('No combinations to generate.');
    btn.innerHTML = '<svg viewBox="0 0 15 15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="3,2 13,7.5 3,13"/></svg> Generate in ETABS';
    return;
  }

  try {
    const res  = await authFetch('/api/load-combinations/generate-batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ combinations })
    });
    if (res.status === 402) {
      const d = (await res.json().catch(() => ({}))).detail || {};
      showGraceModal(d.days || '?', d.message || 'License verification required.');
      return;
    }
    if (res.status === 403) {
      showUpgradeModal('Load Combinations');
      return;
    }
    const data = await res.json();
    statusEl.classList.remove('hidden', 'ok', 'err');
    if (data.status === 'success') {
      statusEl.classList.add('ok');
      statusEl.textContent = '✔ ' + data.message;
      showToast(data.message);
    } else {
      statusEl.classList.add('err');
      statusEl.textContent = '✘ ' + (data.detail || 'Error');
    }
  } catch (e) {
    statusEl.classList.remove('hidden');
    statusEl.classList.add('err');
    statusEl.textContent = '✘ ' + e.message;
    showToast('Error: ' + e.message);
  } finally {
    btn.innerHTML = '<svg viewBox="0 0 15 15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="3,2 13,7.5 3,13"/></svg> Generate in ETABS';
  }
}

// ── Autocomplete ──
function lcShowAc(input, rowEl) {
  const q      = input.value.trim().toLowerCase();
  const popup  = document.getElementById('lc-ac-popup');
  if (!q) { lcHideAc(); return; }

  const matches = LC_TEMPLATES.filter(t => t.name.toLowerCase().includes(q)).slice(0, 12);
  if (!matches.length) { lcHideAc(); return; }

  lcAcRowEl = rowEl;
  lcAcIndex = -1;
  popup.innerHTML = '';

  matches.forEach(tmpl => {
    const item    = document.createElement('div');
    item.className = 'lc-ac-item';
    const preview = Object.entries(tmpl.factors).map(([k, v]) => `${k}=${v}`).join('  ');
    item.innerHTML = `<span class="lc-ac-name">${tmpl.name}</span><span class="lc-ac-factors">${preview}</span>`;
    item.addEventListener('mousedown', e => { e.preventDefault(); lcApplyTemplate(tmpl, rowEl); lcHideAc(); });
    popup.appendChild(item);
  });

  const rect = input.getBoundingClientRect();
  popup.style.left     = rect.left + 'px';
  popup.style.top      = (rect.bottom + 4) + 'px';
  popup.style.minWidth = Math.max(rect.width, 400) + 'px';
  popup.classList.remove('hidden');
}

function lcHideAc() {
  document.getElementById('lc-ac-popup').classList.add('hidden');
  lcAcIndex = -1;
  lcAcRowEl = null;
}

function lcHandleAcKey(e, input, rowEl) {
  const popup = document.getElementById('lc-ac-popup');
  if (popup.classList.contains('hidden')) return;
  const items = popup.querySelectorAll('.lc-ac-item');
  if (!items.length) return;

  if (e.key === 'ArrowDown') {
    e.preventDefault();
    lcAcIndex = Math.min(lcAcIndex + 1, items.length - 1);
    items.forEach((el, i) => el.classList.toggle('lc-ac-active', i === lcAcIndex));
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    lcAcIndex = Math.max(lcAcIndex - 1, 0);
    items.forEach((el, i) => el.classList.toggle('lc-ac-active', i === lcAcIndex));
  } else if (e.key === 'Enter' && lcAcIndex >= 0) {
    e.preventDefault();
    const q = input.value.trim().toLowerCase();
    const m = LC_TEMPLATES.filter(t => t.name.toLowerCase().includes(q));
    if (m[lcAcIndex]) { lcApplyTemplate(m[lcAcIndex], rowEl); lcHideAc(); }
  } else if (e.key === 'Escape') {
    lcHideAc();
  }
}

function lcApplyTemplate(template, rowEl) {
  rowEl.querySelector('.lc-name-input').value = template.name;
  rowEl.querySelectorAll('.lc-factor-input').forEach(inp => {
    const val = template.factors[inp.dataset.col];
    inp.value = val !== undefined ? val : '';
    lcStyleFactor(inp);
  });
}


// ═══════════════════════════════════════════════════════════════════
//  RC BEAM SECTION GENERATOR
// ═══════════════════════════════════════════════════════════════════

let rcbMaterials        = [];   // all material names from ETABS
let rcbSections         = [];   // working rows  [{...}]
let rcbNextNum          = 1;    // auto-increment row number
let rcbSelectedIdx      = -1;   // currently selected row index (-1 = none)
let rcbImportCandidates = [];   // sections fetched but not yet committed

// ── Helpers ──────────────────────────────────────────────────────

function rcbBuildMatOptions(selected = '') {
  if (!rcbMaterials.length) return '<option value="">— no materials —</option>';
  return '<option value="">—</option>' +
    rcbMaterials.map(m =>
      `<option value="${m}" ${m === selected ? 'selected' : ''}>${m}</option>`
    ).join('');
}

function rcbUpdateCount() {
  const n = rcbSections.length;
  document.getElementById('rcb-count').textContent =
    n === 1 ? '1 section' : `${n} sections`;
}

function rcbToggleEmpty() {
  const empty = document.getElementById('rcb-table-empty');
  const wrap  = document.getElementById('rcb-table-wrap');
  if (rcbSections.length === 0) {
    empty.classList.remove('hidden');
    wrap.classList.add('hidden');
  } else {
    empty.classList.add('hidden');
    wrap.classList.remove('hidden');
  }
}

// ── Render / re-render the full table body ────────────────────────

function rcbRenderTable() {
  const tbody = document.getElementById('rcb-tbody');
  if (!tbody) return;

  if (rcbSections.length === 0) {
    tbody.innerHTML = '';
    rcbToggleEmpty();
    rcbUpdateCount();
    return;
  }

  tbody.innerHTML = rcbSections.map((s, idx) => `
    <tr class="rcb-row${idx === rcbSelectedIdx ? ' rcb-row-selected' : ''}" data-idx="${idx}">
      <td class="rcb-num-cell">${s.num}</td>
      <td>
        <input class="rcb-inp" type="text" value="${s.prop_name}"
               data-field="prop_name" data-idx="${idx}"
               placeholder="e.g. G-300x500"/>
      </td>
      <td>
        <select class="rcb-sel" data-field="concrete_strength" data-idx="${idx}">
          ${rcbBuildMatOptions(s.concrete_strength)}
        </select>
      </td>
      <td>
        <select class="rcb-sel" data-field="fy_main" data-idx="${idx}">
          ${rcbBuildMatOptions(s.fy_main)}
        </select>
      </td>
      <td>
        <select class="rcb-sel" data-field="fy_ties" data-idx="${idx}">
          ${rcbBuildMatOptions(s.fy_ties)}
        </select>
      </td>
      <td><input class="rcb-inp rcb-num" type="number" value="${s.depth}"       data-field="depth"       data-idx="${idx}" min="1"/></td>
      <td><input class="rcb-inp rcb-num" type="number" value="${s.width}"       data-field="width"       data-idx="${idx}" min="1"/></td>
      <td><input class="rcb-inp rcb-num" type="number" value="${s.bar_dia}"     data-field="bar_dia"     data-idx="${idx}" min="0"/></td>
      <td><input class="rcb-inp rcb-num" type="number" value="${s.top_cc}"      data-field="top_cc"      data-idx="${idx}" min="0"/></td>
      <td><input class="rcb-inp rcb-num" type="number" value="${s.bot_cc}"      data-field="bot_cc"      data-idx="${idx}" min="0"/></td>
      <td><input class="rcb-inp rcb-num" type="number" value="${s.nbar_top_i}"  data-field="nbar_top_i"  data-idx="${idx}" min="0"/></td>
      <td><input class="rcb-inp rcb-num" type="number" value="${s.nbar_top_j}"  data-field="nbar_top_j"  data-idx="${idx}" min="0"/></td>
      <td><input class="rcb-inp rcb-num" type="number" value="${s.nbar_bot_i}"  data-field="nbar_bot_i"  data-idx="${idx}" min="0"/></td>
      <td><input class="rcb-inp rcb-num" type="number" value="${s.nbar_bot_j}"  data-field="nbar_bot_j"  data-idx="${idx}" min="0"/></td>
      <td><input class="rcb-inp rcb-num" type="number" value="${s.torsion}"     data-field="torsion"     data-idx="${idx}" step="0.001" min="0"/></td>
      <td><input class="rcb-inp rcb-num" type="number" value="${s.i22}"         data-field="i22"         data-idx="${idx}" step="0.01"  min="0"/></td>
      <td><input class="rcb-inp rcb-num" type="number" value="${s.i33}"         data-field="i33"         data-idx="${idx}" step="0.01"  min="0"/></td>
      <td>
        <button class="rcb-del-btn" data-idx="${idx}" title="Delete row" tabindex="-1">
          <svg width="11" height="11" viewBox="0 0 12 12" fill="none"
               stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <line x1="2" y1="2" x2="10" y2="10"/>
            <line x1="10" y1="2" x2="2" y2="10"/>
          </svg>
        </button>
      </td>
    </tr>`).join('');

  rcbToggleEmpty();
  rcbUpdateCount();
  rcbAttachRowListeners();
}

// ── Generate a unique copy name (strips -N suffix, finds next free) ──

function rcbUniqueCopyName(originalName) {
  const base     = originalName.replace(/-\d+$/, '');
  const existing = new Set(rcbSections.map(s => s.prop_name));
  let n = 2;
  while (existing.has(`${base}-${n}`)) n++;
  return `${base}-${n}`;
}

// ── Wire up input / select / delete listeners ─────────────────────

function rcbAttachRowListeners() {
  const tbody = document.getElementById('rcb-tbody');
  if (!tbody) return;

  // Row click → select (ignore clicks on inputs, selects, buttons)
  tbody.querySelectorAll('.rcb-row').forEach(row => {
    row.addEventListener('click', e => {
      if (e.target.closest('input, select, button')) return;
      const idx = parseInt(row.dataset.idx);
      rcbSelectedIdx = (rcbSelectedIdx === idx) ? -1 : idx; // toggle
      tbody.querySelectorAll('.rcb-row').forEach(r =>
        r.classList.toggle('rcb-row-selected', parseInt(r.dataset.idx) === rcbSelectedIdx)
      );
    });
  });

  // Input/select changes sync back to rcbSections[]
  tbody.querySelectorAll('.rcb-inp, .rcb-sel').forEach(el => {
    el.addEventListener('change', e => {
      const idx   = parseInt(e.target.dataset.idx);
      const field = e.target.dataset.field;
      const val   = e.target.value;
      if (rcbSections[idx] !== undefined) {
        const numericFields = [
          'depth','width','bar_dia','top_cc','bot_cc',
          'nbar_top_i','nbar_top_j','nbar_bot_i','nbar_bot_j',
          'torsion','i22','i33'
        ];
        rcbSections[idx][field] = numericFields.includes(field)
          ? (parseFloat(val) || 0) : val;
      }
    });
  });

  // Delete row buttons
  tbody.querySelectorAll('.rcb-del-btn').forEach(btn => {
    btn.addEventListener('click', e => {
      const idx = parseInt(e.currentTarget.dataset.idx);
      rcbSections.splice(idx, 1);
      rcbRenderTable();
    });
  });
}

// ── Material list rendering ───────────────────────────────────────

function rcbRenderMaterials() {
  const ul = document.getElementById('rcb-mat-list');
  if (!ul) return;
  if (!rcbMaterials.length) {
    ul.innerHTML = '<li class="rcb-mat-empty">No materials found</li>';
    return;
  }
  ul.innerHTML = rcbMaterials.map(m =>
    `<li class="rcb-mat-item" data-mat="${m}">${m}</li>`
  ).join('');

  ul.querySelectorAll('.rcb-mat-item').forEach(li => {
    li.addEventListener('click', () => {
      ul.querySelectorAll('.rcb-mat-item').forEach(x => x.classList.remove('selected'));
      li.classList.add('selected');
      // If a row is currently focused/selected, assign material to it
      const focused = document.querySelector('#rcb-tbody .rcb-row:focus-within');
      if (focused) {
        const idx = parseInt(focused.dataset.idx);
        if (rcbSections[idx] !== undefined) {
          rcbSections[idx].material = li.dataset.mat;
          const sel = focused.querySelector('.rcb-sel-mat');
          if (sel) sel.value = li.dataset.mat;
        }
      }
    });
  });
}

// ── Populate AutoGenerate modal dropdowns ─────────────────────────

function rcbPopulateGenDropdowns() {
  ['rcb-gen-conc','rcb-gen-fym','rcb-gen-fyt'].forEach(id => {
    const sel = document.getElementById(id);
    if (!sel) return;
    sel.innerHTML = '<option value="">— select material —</option>' +
      rcbMaterials.map(m => `<option value="${m}">${m}</option>`).join('');
  });
}

// ── Blank row factory ─────────────────────────────────────────────

function rcbBlankRow(overrides = {}) {
  return {
    num:              rcbNextNum++,
    material:         '',
    prop_name:        '',
    concrete_strength:'',
    fy_main:          '',
    fy_ties:          '',
    depth:            500,
    width:            300,
    bar_dia:          25,
    top_cc:           40,
    bot_cc:           40,
    nbar_top_i:       0,
    nbar_top_j:       0,
    nbar_bot_i:       0,
    nbar_bot_j:       0,
    torsion:          0.01,
    i22:              0.35,
    i33:              0.35,
    ...overrides
  };
}

// ── API calls ─────────────────────────────────────────────────────

async function rcbImportMaterials() {
  const btn = document.getElementById('rcb-btn-import-mat');
  btn.disabled = true;
  btn.textContent = 'Loading…';
  try {
    const res = await authFetch('/api/rc-beam/materials');
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showToast(err.detail || 'Failed to import materials', 'error');
      return;
    }
    const data = await res.json();
    rcbMaterials = data.materials || [];
    rcbRenderMaterials();
    rcbPopulateGenDropdowns();
    // refresh dropdowns in existing rows
    if (rcbSections.length) rcbRenderTable();
    showToast(`Loaded ${rcbMaterials.length} material(s)`, 'success');
  } catch (e) {
    showToast('Import materials failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 16 16" fill="none"
      stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <path d="M8 2v9M4 7l4 4 4-4"/><rect x="2" y="13" width="12" height="1.5" rx="0.75"/>
    </svg> Import Material`;
  }
}

async function rcbImportSections() {
  const btn = document.getElementById('rcb-btn-import-sec');
  btn.disabled = true;
  btn.textContent = 'Loading…';
  try {
    const res = await authFetch('/api/rc-beam/sections');
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showToast(err.detail || 'Failed to import sections', 'error');
      return;
    }
    const data = await res.json();
    const sections = data.sections || [];
    if (!sections.length) {
      showToast('No rectangular frame sections found in ETABS model', 'error');
      return;
    }
    rcbImportCandidates = sections;
    rcbOpenImportPicker();
  } catch (e) {
    showToast('Import sections failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 16 16" fill="none"
      stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <rect x="2" y="2" width="12" height="12" rx="1.5"/>
      <line x1="2" y1="6" x2="14" y2="6"/>
      <line x1="6" y1="6" x2="6" y2="14"/>
    </svg> Import Section`;
  }
}

// ── Import picker modal ───────────────────────────────────────────

function rcbOpenImportPicker() {
  const modal = document.getElementById('rcb-import-modal');
  document.getElementById('rcb-import-search').value = '';
  document.getElementById('rcb-import-subtitle').textContent =
    `${rcbImportCandidates.length} rectangular section(s) found`;
  modal.classList.remove('hidden');
  rcbRenderImportList('');
}

function rcbCloseImportPicker() {
  document.getElementById('rcb-import-modal').classList.add('hidden');
}

function rcbRenderImportList(filter) {
  const list   = document.getElementById('rcb-import-list');
  const lc     = filter.toLowerCase();
  const visible = rcbImportCandidates.filter(s =>
    !lc || s.prop_name.toLowerCase().includes(lc)
  );

  if (!visible.length) {
    list.innerHTML = `<div class="rcb-import-empty">No sections match "${filter}"</div>`;
    rcbUpdateImportCount();
    return;
  }

  list.innerHTML = visible.map(s => `
    <label class="rcb-import-item">
      <input type="checkbox" class="rcb-import-chk" value="${s.prop_name}" checked/>
      <span class="rcb-import-name">${s.prop_name}</span>
      <span class="rcb-import-dim">${s.depth} × ${s.width} mm</span>
    </label>`).join('');

  list.querySelectorAll('.rcb-import-chk').forEach(chk =>
    chk.addEventListener('change', rcbUpdateImportCount)
  );
  rcbUpdateImportCount();
}

function rcbUpdateImportCount() {
  const checked = document.querySelectorAll('#rcb-import-list .rcb-import-chk:checked').length;
  const total   = document.querySelectorAll('#rcb-import-list .rcb-import-chk').length;
  const countEl = document.getElementById('rcb-import-count');
  if (countEl) countEl.textContent = `${checked} of ${total} selected`;
}

function rcbConfirmImport() {
  const checked = new Set(
    Array.from(document.querySelectorAll('#rcb-import-list .rcb-import-chk:checked'))
         .map(c => c.value)
  );
  if (!checked.size) {
    showToast('No sections selected', 'warn');
    return;
  }
  // Keep only sections that are checked; re-number from 1
  const imported = rcbImportCandidates
    .filter(s => checked.has(s.prop_name))
    .map((s, i) => ({ ...s, num: i + 1 }));

  rcbSections    = imported;
  rcbNextNum     = imported.length + 1;
  rcbSelectedIdx = -1;
  rcbRenderTable();
  rcbCloseImportPicker();
  showToast(`Imported ${imported.length} section(s) from ETABS`, 'success');
}

async function rcbWriteToETABS() {
  if (!rcbSections.length) {
    showToast('No sections to write. Add or import sections first.', 'error');
    return;
  }
  const btn = document.getElementById('rcb-btn-write');
  btn.disabled = true;
  btn.textContent = 'Writing…';
  try {
    // Collect latest values from DOM inputs (in case user didn't trigger change)
    rcbSyncFromDOM();
    const res = await authFetch('/api/rc-beam/write', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sections: rcbSections }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showToast(err.detail || 'Write to ETABS failed', 'error');
      return;
    }
    const data = await res.json();
    const ok  = data.success_count || 0;
    const bad = data.error_count   || 0;
    if (bad > 0) {
      showToast(`Written: ${ok} ✓  Errors: ${bad} ✗ — check section names & materials`, 'error');
    } else {
      showToast(`${ok} section(s) written to ETABS successfully`, 'success');
    }
  } catch (e) {
    showToast('Write failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 16 16" fill="none"
      stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <path d="M8 11V2M4 7l4-4 4 4"/>
      <rect x="2" y="12" width="12" height="2" rx="1"/>
    </svg> Write to ETABS`;
  }
}

// Sync input values to rcbSections[] from DOM (in case change events weren't fired)
function rcbSyncFromDOM() {
  const tbody = document.getElementById('rcb-tbody');
  if (!tbody) return;
  const numericFields = [
    'depth','width','bar_dia','top_cc','bot_cc',
    'nbar_top_i','nbar_top_j','nbar_bot_i','nbar_bot_j',
    'torsion','i22','i33'
  ];
  tbody.querySelectorAll('.rcb-inp, .rcb-sel').forEach(el => {
    const idx   = parseInt(el.dataset.idx);
    const field = el.dataset.field;
    if (rcbSections[idx] === undefined || !field) return;
    const val = el.value;
    rcbSections[idx][field] = numericFields.includes(field)
      ? (parseFloat(val) || 0) : val;
  });
}

// ── AutoGenerate modal helpers ────────────────────────────────────

function rcbOpenAutogen() {
  rcbPopulateGenDropdowns();
  // Auto-sync name when depth/width changes
  ['rcb-gen-depth','rcb-gen-width'].forEach(id => {
    document.getElementById(id).addEventListener('input', rcbUpdateGenName);
  });
  document.getElementById('rcb-autogen-modal').classList.remove('hidden');
}

function rcbUpdateGenName() {
  const depth = document.getElementById('rcb-gen-depth').value || '';
  const width = document.getElementById('rcb-gen-width').value || '';
  const nameEl = document.getElementById('rcb-gen-name');
  if (!nameEl.dataset.userEdited) {
    nameEl.value = depth && width ? `G-${width}x${depth}` : '';
  }
}

function rcbCloseAutogen() {
  document.getElementById('rcb-autogen-modal').classList.add('hidden');
  // Reset user-edited flag
  const nameEl = document.getElementById('rcb-gen-name');
  if (nameEl) { nameEl.value = ''; delete nameEl.dataset.userEdited; }
}

function rcbConfirmAutogen() {
  const name    = document.getElementById('rcb-gen-name').value.trim();
  const depth   = parseFloat(document.getElementById('rcb-gen-depth').value)   || 500;
  const width   = parseFloat(document.getElementById('rcb-gen-width').value)   || 300;
  const conc    = document.getElementById('rcb-gen-conc').value;
  const fym     = document.getElementById('rcb-gen-fym').value;
  const fyt     = document.getElementById('rcb-gen-fyt').value;
  const bardia  = parseFloat(document.getElementById('rcb-gen-bardia').value)  || 25;
  const topcc   = parseFloat(document.getElementById('rcb-gen-topcc').value)   || 40;
  const botcc   = parseFloat(document.getElementById('rcb-gen-botcc').value)   || 40;
  const torsion = parseFloat(document.getElementById('rcb-gen-torsion').value) || 0.01;
  const i22     = parseFloat(document.getElementById('rcb-gen-i22').value)     || 0.35;
  const i33     = parseFloat(document.getElementById('rcb-gen-i33').value)     || 0.35;

  const autoName = name || `G-${width}x${depth}`;

  rcbSections.push(rcbBlankRow({
    prop_name:        autoName,
    material:         conc,
    concrete_strength: conc,
    fy_main:          fym,
    fy_ties:          fyt,
    depth, width, bar_dia: bardia,
    top_cc: topcc, bot_cc: botcc,
    torsion, i22, i33,
  }));
  rcbRenderTable();
  rcbCloseAutogen();
  showToast(`Section "${autoName}" added`, 'success');
}

// ── AutoGenerate (range) modal ────────────────────────────────────

function rcbOpenAutoGenRange() {
  // Populate material dropdowns from the loaded materials list
  ['rcbr-conc', 'rcbr-fym', 'rcbr-fyt'].forEach(id => {
    const sel = document.getElementById(id);
    if (!sel) return;
    const cur = sel.value;
    sel.innerHTML = '<option value="">— Select Material —</option>' +
      rcbMaterials.map(m => `<option${m === cur ? ' selected' : ''}>${m}</option>`).join('');
    if (cur) sel.value = cur;
  });
  document.getElementById('rcb-agr-modal').classList.remove('hidden');
}

function rcbCloseAutoGenRange() {
  document.getElementById('rcb-agr-modal').classList.add('hidden');
}

function rcbConfirmAutoGenRange() {
  const conc    = document.getElementById('rcbr-conc').value;
  const fym     = document.getElementById('rcbr-fym').value;
  const fyt     = document.getElementById('rcbr-fyt').value;
  const topcc   = parseFloat(document.getElementById('rcbr-topcc').value)   || 40;
  const botcc   = parseFloat(document.getElementById('rcbr-botcc').value)   || 40;
  const bardia  = parseFloat(document.getElementById('rcbr-bardia').value)  || 25;
  const minW    = parseFloat(document.getElementById('rcbr-min-w').value);
  const maxW    = parseFloat(document.getElementById('rcbr-max-w').value);
  const minD    = parseFloat(document.getElementById('rcbr-min-d').value);
  const maxD    = parseFloat(document.getElementById('rcbr-max-d').value);
  const useWInc = document.getElementById('rcbr-chk-w').checked;
  const useDInc = document.getElementById('rcbr-chk-d').checked;
  const wInc    = parseFloat(document.getElementById('rcbr-inc-w').value)   || 100;
  const dInc    = parseFloat(document.getElementById('rcbr-inc-d').value)   || 100;
  const torsion = parseFloat(document.getElementById('rcbr-torsion').value) || 0.01;
  const i22     = parseFloat(document.getElementById('rcbr-i22').value)     || 0.35;
  const i33     = parseFloat(document.getElementById('rcbr-i33').value)     || 0.35;

  if (!minW || !minD) {
    showToast('Enter at least Minimum Beam Width and Depth', 'error');
    return;
  }

  // Build width array
  const widths = [];
  const effectiveMaxW = (maxW && maxW >= minW) ? maxW : minW;
  if (useWInc && wInc > 0 && effectiveMaxW > minW) {
    for (let w = minW; w <= effectiveMaxW + 0.001; w += wInc) widths.push(Math.round(w));
  } else {
    widths.push(Math.round(minW));
    if (effectiveMaxW > minW) widths.push(Math.round(effectiveMaxW));
  }

  // Build depth array
  const depths = [];
  const effectiveMaxD = (maxD && maxD >= minD) ? maxD : minD;
  if (useDInc && dInc > 0 && effectiveMaxD > minD) {
    for (let d = minD; d <= effectiveMaxD + 0.001; d += dInc) depths.push(Math.round(d));
  } else {
    depths.push(Math.round(minD));
    if (effectiveMaxD > minD) depths.push(Math.round(effectiveMaxD));
  }

  // Generate width × depth combinations, skip duplicates
  const existing = new Set(rcbSections.map(s => s.prop_name));
  const added    = [];
  for (const w of widths) {
    for (const d of depths) {
      const name = `G-${w}x${d}`;
      if (existing.has(name)) continue;
      existing.add(name);
      added.push(rcbBlankRow({
        prop_name: name, material: conc, concrete_strength: conc,
        fy_main: fym, fy_ties: fyt,
        depth: d, width: w,
        bar_dia: bardia, top_cc: topcc, bot_cc: botcc,
        torsion, i22, i33,
      }));
    }
  }

  if (!added.length) {
    showToast('No new sections generated (all names already exist)', 'warn');
    return;
  }
  rcbSections.push(...added);
  rcbSelectedIdx = -1;
  rcbRenderTable();
  rcbCloseAutoGenRange();
  showToast(`Generated ${added.length} section(s)`, 'success');
}

// ── Init / event wiring ───────────────────────────────────────────

function initRcBeam() {
  // Toolbar buttons
  document.getElementById('rcb-btn-import-mat')
    ?.addEventListener('click', rcbImportMaterials);

  document.getElementById('rcb-btn-import-sec')
    ?.addEventListener('click', rcbImportSections);

  document.getElementById('rcb-btn-write')
    ?.addEventListener('click', rcbWriteToETABS);

  document.getElementById('rcb-btn-clear')
    ?.addEventListener('click', () => {
      if (rcbSections.length && !confirm('Clear all sections?')) return;
      rcbSections    = [];
      rcbNextNum     = 1;
      rcbSelectedIdx = -1;
      rcbRenderTable();
      showToast('Table cleared', 'success');
    });

  document.getElementById('rcb-btn-autogen')
    ?.addEventListener('click', rcbOpenAutogen);

  document.getElementById('rcb-btn-autogen-range')
    ?.addEventListener('click', rcbOpenAutoGenRange);

  document.getElementById('rcb-btn-add-row')
    ?.addEventListener('click', () => {
      rcbSections.push(rcbBlankRow());
      rcbSelectedIdx = -1;
      rcbRenderTable();
      // scroll table to bottom
      const wrap = document.getElementById('rcb-table-wrap');
      if (wrap) wrap.scrollTop = wrap.scrollHeight;
    });

  document.getElementById('rcb-btn-add-copy')
    ?.addEventListener('click', () => {
      if (rcbSelectedIdx < 0 || !rcbSections[rcbSelectedIdx]) {
        showToast('Select a row to copy first', 'warn');
        return;
      }
      const src  = rcbSections[rcbSelectedIdx];
      const copy = { ...src, num: rcbNextNum++, prop_name: rcbUniqueCopyName(src.prop_name) };
      rcbSections.push(copy);
      rcbSelectedIdx = rcbSections.length - 1;  // select the new copy
      rcbRenderTable();
      const wrap = document.getElementById('rcb-table-wrap');
      if (wrap) wrap.scrollTop = wrap.scrollHeight;
      showToast(`Copied as "${copy.prop_name}"`, 'success');
    });

  // Import Section picker modal
  document.getElementById('rcb-import-close')
    ?.addEventListener('click', rcbCloseImportPicker);
  document.getElementById('rcb-import-cancel')
    ?.addEventListener('click', rcbCloseImportPicker);
  document.getElementById('rcb-import-confirm')
    ?.addEventListener('click', rcbConfirmImport);
  document.getElementById('rcb-import-modal')
    ?.addEventListener('click', e => {
      if (e.target === e.currentTarget) rcbCloseImportPicker();
    });

  document.getElementById('rcb-import-search')
    ?.addEventListener('input', e => rcbRenderImportList(e.target.value.trim()));

  document.getElementById('rcb-import-all')
    ?.addEventListener('click', () => {
      document.querySelectorAll('#rcb-import-list .rcb-import-chk')
              .forEach(c => { c.checked = true; });
      rcbUpdateImportCount();
    });
  document.getElementById('rcb-import-none')
    ?.addEventListener('click', () => {
      document.querySelectorAll('#rcb-import-list .rcb-import-chk')
              .forEach(c => { c.checked = false; });
      rcbUpdateImportCount();
    });
  document.getElementById('rcb-import-invert')
    ?.addEventListener('click', () => {
      document.querySelectorAll('#rcb-import-list .rcb-import-chk')
              .forEach(c => { c.checked = !c.checked; });
      rcbUpdateImportCount();
    });

  // AutoGenerate modal
  document.getElementById('rcb-autogen-close')
    ?.addEventListener('click', rcbCloseAutogen);
  document.getElementById('rcb-autogen-cancel')
    ?.addEventListener('click', rcbCloseAutogen);
  document.getElementById('rcb-autogen-confirm')
    ?.addEventListener('click', rcbConfirmAutogen);

  // Mark name as user-edited so auto-name stops overwriting
  document.getElementById('rcb-gen-name')
    ?.addEventListener('input', function() {
      this.dataset.userEdited = this.value ? '1' : '';
    });

  // Dismiss modal on backdrop click
  document.getElementById('rcb-autogen-modal')
    ?.addEventListener('click', e => {
      if (e.target === e.currentTarget) rcbCloseAutogen();
    });

  // AutoGenerate (range) modal wiring
  document.getElementById('rcb-agr-close')
    ?.addEventListener('click', rcbCloseAutoGenRange);
  document.getElementById('rcb-agr-cancel')
    ?.addEventListener('click', rcbCloseAutoGenRange);
  document.getElementById('rcb-agr-confirm')
    ?.addEventListener('click', rcbConfirmAutoGenRange);
  document.getElementById('rcb-agr-modal')
    ?.addEventListener('click', e => {
      if (e.target === e.currentTarget) rcbCloseAutoGenRange();
    });

  // Increment checkboxes → enable/disable their selects
  ['w', 'd'].forEach(axis => {
    const chk = document.getElementById(`rcbr-chk-${axis}`);
    const sel = document.getElementById(`rcbr-inc-${axis}`);
    if (chk && sel) {
      chk.addEventListener('change', () => {
        sel.disabled = !chk.checked;
        sel.style.opacity = chk.checked ? '1' : '0.4';
      });
      sel.style.opacity = '0.4'; // initially disabled
    }
  });

  // Initial render
  rcbRenderTable();
}

// ── Bootstrap (called from main DOMContentLoaded) ─────────────────
document.addEventListener('DOMContentLoaded', initRcBeam);


// ═══════════════════════════════════════════════════════════════════
//  RC COLUMN SECTION GENERATOR
// ═══════════════════════════════════════════════════════════════════

let rccMaterials        = [];   // all material names from ETABS
let rccSections         = [];   // working rows  [{...}]
let rccNextNum          = 1;    // auto-increment row number
let rccSelectedIdx      = -1;   // currently selected row index (-1 = none)
let rccImportCandidates = [];   // sections fetched but not yet committed
let rccViewIdx          = 0;    // current section index shown in view modal

// ── Helpers ──────────────────────────────────────────────────────

function rccBuildMatOptions(selected = '') {
  if (!rccMaterials.length) return '<option value="">— no materials —</option>';
  return '<option value="">—</option>' +
    rccMaterials.map(m =>
      `<option value="${m}" ${m === selected ? 'selected' : ''}>${m}</option>`
    ).join('');
}

function rccUpdateCount() {
  const n = rccSections.length;
  document.getElementById('rcc-count').textContent =
    n === 1 ? '1 section' : `${n} sections`;
}

function rccToggleEmpty() {
  const empty = document.getElementById('rcc-table-empty');
  const wrap  = document.getElementById('rcc-table-wrap');
  if (rccSections.length === 0) {
    empty.classList.remove('hidden');
    wrap.classList.add('hidden');
  } else {
    empty.classList.add('hidden');
    wrap.classList.remove('hidden');
  }
}

// ── Render / re-render the full table body ────────────────────────

function rccRenderTable() {
  const tbody = document.getElementById('rcc-tbody');
  if (!tbody) return;

  if (rccSections.length === 0) {
    tbody.innerHTML = '';
    rccToggleEmpty();
    rccUpdateCount();
    return;
  }

  tbody.innerHTML = rccSections.map((s, idx) => `
    <tr class="rcc-row${idx === rccSelectedIdx ? ' rcc-row-selected' : ''}" data-idx="${idx}">
      <td class="rcc-num-cell">${s.num}</td>
      <td>
        <input class="rcc-inp" type="text" value="${s.prop_name}"
               data-field="prop_name" data-idx="${idx}"
               placeholder="e.g. C-500x500"/>
      </td>
      <td>
        <select class="rcc-sel" data-field="concrete_strength" data-idx="${idx}">
          ${rccBuildMatOptions(s.concrete_strength)}
        </select>
      </td>
      <td>
        <select class="rcc-sel" data-field="fy_main" data-idx="${idx}">
          ${rccBuildMatOptions(s.fy_main)}
        </select>
      </td>
      <td>
        <select class="rcc-sel" data-field="fy_ties" data-idx="${idx}">
          ${rccBuildMatOptions(s.fy_ties)}
        </select>
      </td>
      <td><input class="rcc-inp rcc-num" type="number" value="${s.depth}"        data-field="depth"        data-idx="${idx}" min="1"/></td>
      <td><input class="rcc-inp rcc-num" type="number" value="${s.width}"        data-field="width"        data-idx="${idx}" min="1"/></td>
      <td><input class="rcc-inp rcc-num" type="number" value="${s.cover}"        data-field="cover"        data-idx="${idx}" min="0"/></td>
      <td><input class="rcc-inp rcc-num" type="number" value="${s.rebar_size}"   data-field="rebar_size"   data-idx="${idx}" min="0"/></td>
      <td><input class="rcc-inp rcc-num" type="number" value="${s.nbars_3}"      data-field="nbars_3"      data-idx="${idx}" min="2"/></td>
      <td><input class="rcc-inp rcc-num" type="number" value="${s.nbars_2}"      data-field="nbars_2"      data-idx="${idx}" min="2"/></td>
      <td><input class="rcc-inp rcc-num" type="number" value="${s.tie_size}"     data-field="tie_size"     data-idx="${idx}" min="0"/></td>
      <td><input class="rcc-inp rcc-num" type="number" value="${s.tie_spacing}"  data-field="tie_spacing"  data-idx="${idx}" min="1"/></td>
      <td><input class="rcc-inp rcc-num" type="number" value="${s.num_tie_3}"    data-field="num_tie_3"    data-idx="${idx}" min="1"/></td>
      <td><input class="rcc-inp rcc-num" type="number" value="${s.num_tie_2}"    data-field="num_tie_2"    data-idx="${idx}" min="1"/></td>
      <td>
        <input type="checkbox" class="rcc-chk" data-field="to_be_designed" data-idx="${idx}"
               ${s.to_be_designed ? 'checked' : ''}/>
      </td>
      <td><input class="rcc-inp rcc-num" type="number" value="${s.torsion}"      data-field="torsion"      data-idx="${idx}" step="0.001" min="0"/></td>
      <td><input class="rcc-inp rcc-num" type="number" value="${s.i22}"          data-field="i22"          data-idx="${idx}" step="0.01"  min="0"/></td>
      <td><input class="rcc-inp rcc-num" type="number" value="${s.i33}"          data-field="i33"          data-idx="${idx}" step="0.01"  min="0"/></td>
      <td>
        <button class="rcc-del-btn" data-idx="${idx}" title="Delete row" tabindex="-1">
          <svg width="11" height="11" viewBox="0 0 12 12" fill="none"
               stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <line x1="2" y1="2" x2="10" y2="10"/>
            <line x1="10" y1="2" x2="2" y2="10"/>
          </svg>
        </button>
      </td>
    </tr>`).join('');

  rccToggleEmpty();
  rccUpdateCount();
  rccAttachRowListeners();
}

// ── Unique copy name ──────────────────────────────────────────────

function rccUniqueCopyName(originalName) {
  const base     = originalName.replace(/-\d+$/, '');
  const existing = new Set(rccSections.map(s => s.prop_name));
  let n = 2;
  while (existing.has(`${base}-${n}`)) n++;
  return `${base}-${n}`;
}

// ── Wire up row listeners ─────────────────────────────────────────

function rccAttachRowListeners() {
  const tbody = document.getElementById('rcc-tbody');
  if (!tbody) return;

  tbody.querySelectorAll('.rcc-row').forEach(row => {
    row.addEventListener('click', e => {
      if (e.target.closest('input, select, button')) return;
      const idx = parseInt(row.dataset.idx);
      rccSelectedIdx = (rccSelectedIdx === idx) ? -1 : idx;
      tbody.querySelectorAll('.rcc-row').forEach(r =>
        r.classList.toggle('rcc-row-selected', parseInt(r.dataset.idx) === rccSelectedIdx)
      );
    });
  });

  const numericFields = [
    'depth','width','cover','rebar_size','nbars_3','nbars_2',
    'tie_size','tie_spacing','num_tie_3','num_tie_2','torsion','i22','i33'
  ];

  tbody.querySelectorAll('.rcc-inp, .rcc-sel').forEach(el => {
    el.addEventListener('change', e => {
      const idx   = parseInt(e.target.dataset.idx);
      const field = e.target.dataset.field;
      const val   = e.target.value;
      if (rccSections[idx] !== undefined) {
        rccSections[idx][field] = numericFields.includes(field)
          ? (parseFloat(val) || 0) : val;
      }
    });
  });

  tbody.querySelectorAll('.rcc-chk').forEach(el => {
    el.addEventListener('change', e => {
      const idx = parseInt(e.target.dataset.idx);
      if (rccSections[idx] !== undefined) {
        rccSections[idx]['to_be_designed'] = e.target.checked;
      }
    });
  });

  tbody.querySelectorAll('.rcc-del-btn').forEach(btn => {
    btn.addEventListener('click', e => {
      const idx = parseInt(e.currentTarget.dataset.idx);
      rccSections.splice(idx, 1);
      rccRenderTable();
    });
  });
}

// ── Material list rendering ───────────────────────────────────────

function rccRenderMaterials() {
  const ul = document.getElementById('rcc-mat-list');
  if (!ul) return;
  if (!rccMaterials.length) {
    ul.innerHTML = '<li class="rcc-mat-empty">No materials found</li>';
    return;
  }
  ul.innerHTML = rccMaterials.map(m =>
    `<li class="rcc-mat-item" data-mat="${m}">${m}</li>`
  ).join('');

  ul.querySelectorAll('.rcc-mat-item').forEach(li => {
    li.addEventListener('click', () => {
      ul.querySelectorAll('.rcc-mat-item').forEach(x => x.classList.remove('selected'));
      li.classList.add('selected');
    });
  });
}

// ── Populate Add Column modal dropdowns ───────────────────────────

function rccPopulateGenDropdowns() {
  ['rcc-gen-conc','rcc-gen-fym','rcc-gen-fyt'].forEach(id => {
    const sel = document.getElementById(id);
    if (!sel) return;
    sel.innerHTML = '<option value="">— select material —</option>' +
      rccMaterials.map(m => `<option value="${m}">${m}</option>`).join('');
  });
}

// ── Blank row factory ─────────────────────────────────────────────

function rccBlankRow(overrides = {}) {
  return {
    num:              rccNextNum++,
    material:         '',
    prop_name:        '',
    concrete_strength:'',
    fy_main:          '',
    fy_ties:          '',
    depth:            500,
    width:            500,
    cover:            40,
    rebar_size:       28,
    nbars_3:          3,
    nbars_2:          3,
    tie_size:         12,
    tie_spacing:      150,
    num_tie_3:        3,
    num_tie_2:        3,
    to_be_designed:   false,
    torsion:          0.01,
    i22:              0.70,
    i33:              0.70,
    ...overrides
  };
}

// ── API calls ─────────────────────────────────────────────────────

async function rccImportMaterials() {
  const btn = document.getElementById('rcc-btn-import-mat');
  btn.disabled = true;
  btn.textContent = 'Loading…';
  try {
    const res = await authFetch('/api/rc-column/materials');
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showToast(err.detail || 'Failed to import materials', 'error');
      return;
    }
    const data = await res.json();
    rccMaterials = data.materials || [];
    rccRenderMaterials();
    rccPopulateGenDropdowns();
    if (rccSections.length) rccRenderTable();
    showToast(`Loaded ${rccMaterials.length} material(s)`, 'success');
  } catch (e) {
    showToast('Import materials failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 16 16" fill="none"
      stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <path d="M8 2v9M4 7l4 4 4-4"/><rect x="2" y="13" width="12" height="1.5" rx="0.75"/>
    </svg> Import Material`;
  }
}

async function rccImportSections() {
  const btn = document.getElementById('rcc-btn-import-sec');
  btn.disabled = true;
  btn.textContent = 'Loading…';
  try {
    const res = await authFetch('/api/rc-column/sections');
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showToast(err.detail || 'Failed to import sections', 'error');
      return;
    }
    const data = await res.json();
    const sections = data.sections || [];
    if (!sections.length) {
      showToast('No rectangular frame sections found in ETABS model', 'error');
      return;
    }
    rccImportCandidates = sections;
    rccOpenImportPicker();
  } catch (e) {
    showToast('Import sections failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 16 16" fill="none"
      stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <rect x="2" y="2" width="12" height="12" rx="1.5"/>
      <line x1="2" y1="6" x2="14" y2="6"/>
      <line x1="6" y1="6" x2="6" y2="14"/>
    </svg> Import Section`;
  }
}

async function rccWriteToETABS() {
  if (!rccSections.length) {
    showToast('No sections to write. Add or import sections first.', 'error');
    return;
  }
  const btn = document.getElementById('rcc-btn-write');
  btn.disabled = true;
  btn.textContent = 'Writing…';
  try {
    rccSyncFromDOM();
    const res = await authFetch('/api/rc-column/write', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sections: rccSections }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showToast(err.detail || 'Write to ETABS failed', 'error');
      return;
    }
    const data = await res.json();
    const ok  = data.success_count || 0;
    const bad = data.error_count   || 0;
    if (bad > 0) {
      showToast(`Written: ${ok} ✓  Errors: ${bad} ✗ — check section names & materials`, 'error');
    } else {
      showToast(`${ok} column section(s) written to ETABS successfully`, 'success');
    }
  } catch (e) {
    showToast('Write failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 16 16" fill="none"
      stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <path d="M8 11V2M4 7l4-4 4 4"/>
      <rect x="2" y="12" width="12" height="2" rx="1"/>
    </svg> Write to ETABS`;
  }
}

function rccSyncFromDOM() {
  const tbody = document.getElementById('rcc-tbody');
  if (!tbody) return;
  const numericFields = [
    'depth','width','cover','rebar_size','nbars_3','nbars_2',
    'tie_size','tie_spacing','num_tie_3','num_tie_2','torsion','i22','i33'
  ];
  tbody.querySelectorAll('.rcc-inp, .rcc-sel').forEach(el => {
    const idx   = parseInt(el.dataset.idx);
    const field = el.dataset.field;
    if (rccSections[idx] === undefined || !field) return;
    const val = el.value;
    rccSections[idx][field] = numericFields.includes(field)
      ? (parseFloat(val) || 0) : val;
  });
  tbody.querySelectorAll('.rcc-chk').forEach(el => {
    const idx = parseInt(el.dataset.idx);
    if (rccSections[idx] !== undefined) {
      rccSections[idx]['to_be_designed'] = el.checked;
    }
  });
}

// ── Import picker modal ───────────────────────────────────────────

function rccOpenImportPicker() {
  const modal = document.getElementById('rcc-import-modal');
  document.getElementById('rcc-import-search').value = '';
  document.getElementById('rcc-import-subtitle').textContent =
    `${rccImportCandidates.length} rectangular section(s) found`;
  modal.classList.remove('hidden');
  rccRenderImportList('');
}

function rccCloseImportPicker() {
  document.getElementById('rcc-import-modal').classList.add('hidden');
}

function rccRenderImportList(filter) {
  const list   = document.getElementById('rcc-import-list');
  const lc     = filter.toLowerCase();
  const visible = rccImportCandidates.filter(s =>
    !lc || s.prop_name.toLowerCase().includes(lc)
  );

  if (!visible.length) {
    list.innerHTML = `<div class="rcc-import-empty">No sections match "${filter}"</div>`;
    rccUpdateImportCount();
    return;
  }

  list.innerHTML = visible.map(s => `
    <label class="rcc-import-item">
      <input type="checkbox" class="rcc-import-chk" value="${s.prop_name}" checked/>
      <span class="rcc-import-name">${s.prop_name}</span>
      <span class="rcc-import-dim">${s.depth} × ${s.width} mm</span>
    </label>`).join('');

  list.querySelectorAll('.rcc-import-chk').forEach(chk =>
    chk.addEventListener('change', rccUpdateImportCount)
  );
  rccUpdateImportCount();
}

function rccUpdateImportCount() {
  const checked = document.querySelectorAll('#rcc-import-list .rcc-import-chk:checked').length;
  const total   = document.querySelectorAll('#rcc-import-list .rcc-import-chk').length;
  const countEl = document.getElementById('rcc-import-count');
  if (countEl) countEl.textContent = `${checked} of ${total} selected`;
}

function rccConfirmImport() {
  const checked = new Set(
    Array.from(document.querySelectorAll('#rcc-import-list .rcc-import-chk:checked'))
         .map(c => c.value)
  );
  if (!checked.size) {
    showToast('No sections selected', 'warn');
    return;
  }
  const imported = rccImportCandidates
    .filter(s => checked.has(s.prop_name))
    .map((s, i) => ({ ...s, num: i + 1 }));

  rccSections    = imported;
  rccNextNum     = imported.length + 1;
  rccSelectedIdx = -1;
  rccRenderTable();
  rccCloseImportPicker();
  showToast(`Imported ${imported.length} section(s) from ETABS`, 'success');
}

// ── Add Column modal ──────────────────────────────────────────────

function rccOpenAddCol() {
  rccPopulateGenDropdowns();
  document.getElementById('rcc-addcol-modal').classList.remove('hidden');
}

function rccUpdateGenName() {
  const depth = document.getElementById('rcc-gen-depth').value || '';
  const width = document.getElementById('rcc-gen-width').value || '';
  const nameEl = document.getElementById('rcc-gen-name');
  if (!nameEl.dataset.userEdited) {
    nameEl.value = depth && width ? `C-${depth}x${width}` : '';
  }
}

function rccCloseAddCol() {
  document.getElementById('rcc-addcol-modal').classList.add('hidden');
  const nameEl = document.getElementById('rcc-gen-name');
  if (nameEl) { nameEl.value = ''; delete nameEl.dataset.userEdited; }
}

function rccConfirmAddCol() {
  const name      = document.getElementById('rcc-gen-name').value.trim();
  const depth     = parseFloat(document.getElementById('rcc-gen-depth').value)    || 500;
  const width     = parseFloat(document.getElementById('rcc-gen-width').value)    || 500;
  const conc      = document.getElementById('rcc-gen-conc').value;
  const fym       = document.getElementById('rcc-gen-fym').value;
  const fyt       = document.getElementById('rcc-gen-fyt').value;
  const cover     = parseFloat(document.getElementById('rcc-gen-cover').value)    || 40;
  const rebarSize = parseFloat(document.getElementById('rcc-gen-rebar').value)    || 28;
  const nb3       = parseInt(document.getElementById('rcc-gen-nb3').value)        || 3;
  const nb2       = parseInt(document.getElementById('rcc-gen-nb2').value)        || 3;
  const tieSize   = parseFloat(document.getElementById('rcc-gen-tie').value)      || 12;
  const spacing   = parseFloat(document.getElementById('rcc-gen-spacing').value)  || 150;
  const torsion   = parseFloat(document.getElementById('rcc-gen-torsion').value)  || 0.01;
  const i22       = parseFloat(document.getElementById('rcc-gen-i22').value)      || 0.70;
  const i33       = parseFloat(document.getElementById('rcc-gen-i33').value)      || 0.70;

  const autoName = name || `C-${depth}x${width}`;

  rccSections.push(rccBlankRow({
    prop_name:         autoName,
    material:          conc,
    concrete_strength: conc,
    fy_main:           fym,
    fy_ties:           fyt,
    depth, width, cover,
    rebar_size: rebarSize, nbars_3: nb3, nbars_2: nb2,
    tie_size: tieSize, tie_spacing: spacing,
    num_tie_3: nb3, num_tie_2: nb2,
    torsion, i22, i33,
  }));
  rccRenderTable();
  rccCloseAddCol();
  showToast(`Section "${autoName}" added`, 'success');
}

// ── View Section modal (SVG cross-section) ────────────────────────

function rccOpenViewModal() {
  if (!rccSections.length) {
    showToast('No sections to view. Add or import sections first.', 'warn');
    return;
  }
  rccSyncFromDOM();

  // Populate selector
  const sel = document.getElementById('rcc-view-sel');
  sel.innerHTML = rccSections.map((s, i) =>
    `<option value="${i}">${s.prop_name || `Section ${s.num}`}</option>`
  ).join('');

  // Default to currently selected row or first
  rccViewIdx = rccSelectedIdx >= 0 ? rccSelectedIdx : 0;
  sel.value  = rccViewIdx;

  document.getElementById('rcc-view-modal').classList.remove('hidden');
  rccDrawSection(rccViewIdx);
}

function rccCloseViewModal() {
  document.getElementById('rcc-view-modal').classList.add('hidden');
}

function rccDrawSection(idx) {
  if (idx < 0 || idx >= rccSections.length) return;
  const s = rccSections[idx];

  // ── Canvas setup ──────────────────────────────────────────────
  // depth (t3) = VERTICAL axis,  width (t2) = HORIZONTAL axis
  const canvasW = 260, canvasH = 340;
  const padL = 32, padR = 16, padT = 28, padB = 28;
  const drawW = canvasW - padL - padR;
  const drawH = canvasH - padT - padB;

  // Scale: fit t2 (width) horizontally, t3 (depth) vertically
  const scaleX = drawW / s.width;
  const scaleY = drawH / s.depth;
  const scale  = Math.min(scaleX, scaleY, 2.0);

  const colW = s.width * scale;    // SVG width  = t2 (width)
  const colH = s.depth * scale;    // SVG height = t3 (depth)
  const ox   = padL + (drawW - colW) / 2;
  const oy   = padT + (drawH - colH) / 2;

  const cvpx = s.cover * scale;
  const barR = Math.max(3, Math.min((s.rebar_size / 2) * scale, 7));

  // ── Grid background ───────────────────────────────────────────
  const gridStep = 20;
  let gridLines = '';
  for (let gx = ox; gx <= ox + colW + 0.5; gx += gridStep) {
    gridLines += `<line x1="${gx.toFixed(1)}" y1="${oy.toFixed(1)}"
                        x2="${gx.toFixed(1)}" y2="${(oy+colH).toFixed(1)}"
                        stroke="#cbd5e1" stroke-width="0.5"/>`;
  }
  for (let gy = oy; gy <= oy + colH + 0.5; gy += gridStep) {
    gridLines += `<line x1="${ox.toFixed(1)}" y1="${gy.toFixed(1)}"
                        x2="${(ox+colW).toFixed(1)}" y2="${gy.toFixed(1)}"
                        stroke="#cbd5e1" stroke-width="0.5"/>`;
  }

  // ── Tie rectangle (at cover) ──────────────────────────────────
  const iL = ox + cvpx, iR = ox + colW - cvpx;
  const iT = oy + cvpx, iB = oy + colH - cvpx;
  const iW = iR - iL,   iH = iB - iT;

  // ── Rebar positions ───────────────────────────────────────────
  // nb3 = bars on the top/bottom faces (the t2/width faces, horizontal)
  // nb2 = bars on the left/right faces (the t3/depth faces, vertical)
  const nb3 = Math.max(2, s.nbars_3);
  const nb2 = Math.max(2, s.nbars_2);

  const bars = [];
  // Top and bottom faces: nb3 bars spaced across the width (iW)
  for (let i = 0; i < nb3; i++) {
    const x = iL + (nb3 > 1 ? (iW / (nb3 - 1)) * i : iW / 2);
    bars.push({ x, y: iT });
    bars.push({ x, y: iB });
  }
  // Left and right faces: nb2 bars spaced along the depth (iH) — corners already added
  for (let i = 1; i < nb2 - 1; i++) {
    const y = iT + (iH / (nb2 - 1)) * i;
    bars.push({ x: iL, y });
    bars.push({ x: iR, y });
  }
  // Deduplicate
  const unique = [];
  bars.forEach(b => {
    if (!unique.some(u => Math.abs(u.x - b.x) < 1 && Math.abs(u.y - b.y) < 1))
      unique.push(b);
  });

  const barDots = unique.map(b =>
    `<circle cx="${b.x.toFixed(1)}" cy="${b.y.toFixed(1)}" r="${barR.toFixed(1)}"
             fill="#ef4444" stroke="#7f1d1d" stroke-width="0.8"/>`
  ).join('');

  // ── Dimension annotations ─────────────────────────────────────
  const lblW = `<text x="${(ox+colW/2).toFixed(1)}" y="${(oy-10).toFixed(1)}"
      text-anchor="middle" font-family="sans-serif" font-size="10" fill="#475569">${s.width} mm</text>`;
  const lblD = `<text x="${(ox-12).toFixed(1)}" y="${(oy+colH/2).toFixed(1)}"
      text-anchor="middle" font-family="sans-serif" font-size="10" fill="#475569"
      transform="rotate(-90 ${(ox-12).toFixed(1)} ${(oy+colH/2).toFixed(1)})">${s.depth} mm</text>`;
  const lblCover = `<text x="${(ox+colW/2).toFixed(1)}" y="${(oy+colH+18).toFixed(1)}"
      text-anchor="middle" font-family="sans-serif" font-size="9" fill="#94a3b8">
      Cover: ${s.cover} mm  |  ⌀${s.rebar_size} mm</text>`;

  // ── Build SVG ─────────────────────────────────────────────────
  const svgContent = `
<clipPath id="col-clip">
  <rect x="${ox.toFixed(1)}" y="${oy.toFixed(1)}" width="${colW.toFixed(1)}" height="${colH.toFixed(1)}"/>
</clipPath>
<rect x="${ox.toFixed(1)}" y="${oy.toFixed(1)}" width="${colW.toFixed(1)}" height="${colH.toFixed(1)}"
      fill="#dbeafe" stroke="none"/>
<g clip-path="url(#col-clip)">${gridLines}</g>
<rect x="${ox.toFixed(1)}" y="${oy.toFixed(1)}" width="${colW.toFixed(1)}" height="${colH.toFixed(1)}"
      fill="none" stroke="#1e293b" stroke-width="2"/>
<rect x="${iL.toFixed(1)}" y="${iT.toFixed(1)}" width="${iW.toFixed(1)}" height="${iH.toFixed(1)}"
      fill="none" stroke="#334155" stroke-width="1.5" stroke-dasharray="5 3" rx="1"/>
${barDots}
${lblW}${lblD}${lblCover}`;

  const svgEl = document.getElementById('rcc-view-svg');
  if (svgEl) {
    svgEl.setAttribute('viewBox', `0 0 ${canvasW} ${canvasH}`);
    svgEl.setAttribute('width',  canvasW);
    svgEl.setAttribute('height', canvasH);
    svgEl.innerHTML = svgContent;
  }

  // ── Section label above drawing ───────────────────────────────
  const name = s.prop_name || `Section ${s.num}`;
  const totalBars = unique.length;
  const labelEl = document.getElementById('rcc-view-label');
  if (labelEl) labelEl.textContent =
    `${name}  —  ${totalBars}-⌀${s.rebar_size}d`;

  // ── Selector sync ─────────────────────────────────────────────
  const sel = document.getElementById('rcc-view-sel');
  if (sel) sel.value = idx;

  // ── Footer bar count ──────────────────────────────────────────
  const bcEl = document.getElementById('rcc-view-barcount');
  if (bcEl) bcEl.textContent = `${totalBars} bars total  •  ${s.depth}×${s.width} mm`;

  // ── Properties panel ─────────────────────────────────────────
  const set = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.textContent = val || '—';
  };
  set('rvp-conc',    s.concrete_strength || s.material);
  set('rvp-fym',     s.fy_main);
  set('rvp-fyt',     s.fy_ties);
  set('rvp-depth',   s.depth + ' mm');
  set('rvp-width',   s.width + ' mm');
  set('rvp-bardia',  s.rebar_size + ' mm');
  set('rvp-cover',   s.cover + ' mm');
  set('rvp-nb3',     s.nbars_3);
  set('rvp-nb2',     s.nbars_2);
  set('rvp-tiesize', s.tie_size + ' mm');
  set('rvp-spacing', s.tie_spacing + ' mm');
  set('rvp-ntie3',   s.num_tie_3);
  set('rvp-ntie2',   s.num_tie_2);
  set('rvp-torsion', s.torsion);
  set('rvp-i22',     s.i22);
  set('rvp-i33',     s.i33);
}

// ── Create Drawing (SVG download) ─────────────────────────────────

function rccCreateDrawing() {
  if (!rccSections.length) {
    showToast('No sections to draw. Add or import sections first.', 'warn');
    return;
  }
  rccSyncFromDOM();

  // Build a multi-section SVG sheet
  const cols    = 3;
  const cellW   = 280, cellH = 300;
  const padX    = 30,  padY  = 30;
  const rows    = Math.ceil(rccSections.length / cols);
  const totalW  = cols * (cellW + padX) + padX;
  const totalH  = rows * (cellH + padY) + padY + 40;  // 40 for title

  let cells = rccSections.map((s, n) => {
    const col  = n % cols;
    const row  = Math.floor(n / cols);
    const ox   = padX + col * (cellW + padX);
    const oy   = 40 + padY + row * (cellH + padY);

    const maxDim = Math.max(s.depth, s.width, 1);
    const scale  = Math.min((cellW - 60) / s.width, (cellH - 80) / s.depth, (cellW - 60) / maxDim);
    const cW     = s.width * scale;
    const cH     = s.depth * scale;
    const cx     = ox + cellW / 2 - cW / 2;
    const cy     = oy + 20;
    const cvmm   = s.cover * scale;
    const barR   = Math.max(2.5, (s.rebar_size / 2) * scale);

    const iL = cx + cvmm, iR = cx + cW - cvmm;
    const iT = cy + cvmm, iB = cy + cH - cvmm;
    const iW = iR - iL,   iH = iB - iT;

    const nb3 = Math.max(2, s.nbars_3);
    const nb2 = Math.max(2, s.nbars_2);
    const bars = [];
    for (let i = 0; i < nb3; i++) {
      const x = iL + (iW / (nb3 - 1)) * i;
      bars.push({ x, y: iT }); bars.push({ x, y: iB });
    }
    for (let i = 1; i < nb2 - 1; i++) {
      const y = iT + (iH / (nb2 - 1)) * i;
      bars.push({ x: iL, y }); bars.push({ x: iR, y });
    }
    const unique = [];
    bars.forEach(b => {
      if (!unique.some(u => Math.abs(u.x - b.x) < 1 && Math.abs(u.y - b.y) < 1))
        unique.push(b);
    });

    return `
<rect x="${cx.toFixed(1)}" y="${cy.toFixed(1)}" width="${cW.toFixed(1)}" height="${cH.toFixed(1)}"
      fill="#e2e8f0" stroke="#334155" stroke-width="1.5"/>
<rect x="${iL.toFixed(1)}" y="${iT.toFixed(1)}" width="${iW.toFixed(1)}" height="${iH.toFixed(1)}"
      fill="none" stroke="#64748b" stroke-width="1" stroke-dasharray="3 2"/>
${unique.map(b => `<circle cx="${b.x.toFixed(1)}" cy="${b.y.toFixed(1)}" r="${barR.toFixed(1)}" fill="#ef4444" stroke="#991b1b" stroke-width="0.6"/>`).join('')}
<text x="${(ox + cellW/2).toFixed(1)}" y="${(cy + cH + 16).toFixed(1)}"
      text-anchor="middle" font-family="sans-serif" font-size="11" fill="#334155">
  ${s.prop_name || `Sec ${s.num}`}
</text>
<text x="${(ox + cellW/2).toFixed(1)}" y="${(cy + cH + 30).toFixed(1)}"
      text-anchor="middle" font-family="sans-serif" font-size="9.5" fill="#64748b">
  ${s.depth}×${s.width}mm  cover=${s.cover}  ⌀${s.rebar_size}  n=${unique.length}
</text>`;
  }).join('');

  const svgStr = `<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="${totalW}" height="${totalH}"
     viewBox="0 0 ${totalW} ${totalH}">
  <rect width="100%" height="100%" fill="white"/>
  <text x="${totalW/2}" y="28" text-anchor="middle"
        font-family="sans-serif" font-size="16" font-weight="bold" fill="#0f172a">
    RC Column Section Generator — Drawing Sheet
  </text>
  ${cells}
</svg>`;

  const blob = new Blob([svgStr], { type: 'image/svg+xml' });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href     = url;
  a.download = 'rc_column_sections.svg';
  a.click();
  URL.revokeObjectURL(url);
  showToast(`Drawing exported: rc_column_sections.svg (${rccSections.length} section(s))`, 'success');
}

// ── Init / event wiring ───────────────────────────────────────────

function initRcColumn() {
  document.getElementById('rcc-btn-import-mat')
    ?.addEventListener('click', rccImportMaterials);

  document.getElementById('rcc-btn-import-sec')
    ?.addEventListener('click', rccImportSections);

  document.getElementById('rcc-btn-write')
    ?.addEventListener('click', rccWriteToETABS);

  document.getElementById('rcc-btn-clear')
    ?.addEventListener('click', () => {
      if (rccSections.length && !confirm('Clear all column sections?')) return;
      rccSections    = [];
      rccNextNum     = 1;
      rccSelectedIdx = -1;
      rccRenderTable();
      showToast('Table cleared', 'success');
    });

  document.getElementById('rcc-btn-add-col')
    ?.addEventListener('click', rccOpenAddCol);

  document.getElementById('rcc-btn-view-sec')
    ?.addEventListener('click', rccOpenViewModal);

  document.getElementById('rcc-btn-drawing')
    ?.addEventListener('click', rccCreateDrawing);

  document.getElementById('rcc-btn-add-row')
    ?.addEventListener('click', () => {
      rccSections.push(rccBlankRow());
      rccSelectedIdx = -1;
      rccRenderTable();
      const wrap = document.getElementById('rcc-table-wrap');
      if (wrap) wrap.scrollTop = wrap.scrollHeight;
    });

  document.getElementById('rcc-btn-add-copy')
    ?.addEventListener('click', () => {
      if (rccSelectedIdx < 0 || !rccSections[rccSelectedIdx]) {
        showToast('Select a row to copy first', 'warn');
        return;
      }
      const src  = rccSections[rccSelectedIdx];
      const copy = { ...src, num: rccNextNum++, prop_name: rccUniqueCopyName(src.prop_name) };
      rccSections.push(copy);
      rccSelectedIdx = rccSections.length - 1;
      rccRenderTable();
      const wrap = document.getElementById('rcc-table-wrap');
      if (wrap) wrap.scrollTop = wrap.scrollHeight;
      showToast(`Copied as "${copy.prop_name}"`, 'success');
    });

  // Import Section picker modal
  document.getElementById('rcc-import-close')
    ?.addEventListener('click', rccCloseImportPicker);
  document.getElementById('rcc-import-cancel')
    ?.addEventListener('click', rccCloseImportPicker);
  document.getElementById('rcc-import-confirm')
    ?.addEventListener('click', rccConfirmImport);
  document.getElementById('rcc-import-modal')
    ?.addEventListener('click', e => {
      if (e.target === e.currentTarget) rccCloseImportPicker();
    });

  document.getElementById('rcc-import-search')
    ?.addEventListener('input', e => rccRenderImportList(e.target.value.trim()));

  document.getElementById('rcc-import-all')
    ?.addEventListener('click', () => {
      document.querySelectorAll('#rcc-import-list .rcc-import-chk')
              .forEach(c => { c.checked = true; });
      rccUpdateImportCount();
    });
  document.getElementById('rcc-import-none')
    ?.addEventListener('click', () => {
      document.querySelectorAll('#rcc-import-list .rcc-import-chk')
              .forEach(c => { c.checked = false; });
      rccUpdateImportCount();
    });
  document.getElementById('rcc-import-invert')
    ?.addEventListener('click', () => {
      document.querySelectorAll('#rcc-import-list .rcc-import-chk')
              .forEach(c => { c.checked = !c.checked; });
      rccUpdateImportCount();
    });

  // Add Column modal
  document.getElementById('rcc-addcol-close')
    ?.addEventListener('click', rccCloseAddCol);
  document.getElementById('rcc-addcol-cancel')
    ?.addEventListener('click', rccCloseAddCol);
  document.getElementById('rcc-addcol-confirm')
    ?.addEventListener('click', rccConfirmAddCol);
  document.getElementById('rcc-addcol-modal')
    ?.addEventListener('click', e => {
      if (e.target === e.currentTarget) rccCloseAddCol();
    });
  document.getElementById('rcc-gen-name')
    ?.addEventListener('input', function() {
      this.dataset.userEdited = this.value ? '1' : '';
    });
  document.getElementById('rcc-gen-depth')
    ?.addEventListener('input', rccUpdateGenName);
  document.getElementById('rcc-gen-width')
    ?.addEventListener('input', rccUpdateGenName);

  // View Sections modal
  document.getElementById('rcc-view-close')
    ?.addEventListener('click', rccCloseViewModal);
  document.getElementById('rcc-view-close2')
    ?.addEventListener('click', rccCloseViewModal);
  document.getElementById('rcc-view-modal')
    ?.addEventListener('click', e => {
      if (e.target === e.currentTarget) rccCloseViewModal();
    });
  document.getElementById('rcc-view-sel')
    ?.addEventListener('change', e => {
      rccViewIdx = parseInt(e.target.value);
      rccDrawSection(rccViewIdx);
    });
  document.getElementById('rcc-view-prev')
    ?.addEventListener('click', () => {
      rccViewIdx = Math.max(0, rccViewIdx - 1);
      rccDrawSection(rccViewIdx);
    });
  document.getElementById('rcc-view-next')
    ?.addEventListener('click', () => {
      rccViewIdx = Math.min(rccSections.length - 1, rccViewIdx + 1);
      rccDrawSection(rccViewIdx);
    });

  // Initial render
  rccRenderTable();
}

document.addEventListener('DOMContentLoaded', initRcColumn);


// ================================================================
//  RUN FILE CLEANER
// ================================================================

let _cleanerLastFiles = [];   // files found on last scan

/** Strip surrounding whitespace and quotes that Windows sometimes adds to pasted paths. */
function cleanerNormPath(raw) {
  return raw.trim().replace(/^["']+|["']+$/g, '').trim();
}

/** Format a byte count as a human-readable string (KB / MB / GB). */
function _fmtBytes(bytes) {
  if (bytes === 0 || bytes == null) return '0 KB';
  if (bytes < 1024 * 1024)         return (bytes / 1024).toFixed(1) + ' KB';
  if (bytes < 1024 * 1024 * 1024)  return (bytes / (1024 * 1024)).toFixed(2) + ' MB';
  return (bytes / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
}

const _BROWSE_ICON = `<svg width="14" height="14" viewBox="0 0 18 18" fill="none"
  stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M2 5.5h6l2 2h6v9H2z"/><line x1="2" y1="5.5" x2="2" y2="14.5"/></svg>`;

async function cleanerBrowse() {
  const btn = document.getElementById('btn-cleaner-browse');
  btn.disabled = true;
  btn.innerHTML = _BROWSE_ICON + ' Opening…';
  showToast('A folder picker window has opened — check your taskbar if you cannot see it.', 'info');
  try {
    const res  = await authFetch('/api/clean/browse-folder');
    const data = await res.json();
    if (!res.ok) { showToast(data.detail || 'Could not open folder picker', 'error'); return; }
    if (data.path) {
      document.getElementById('cleaner-dir-input').value = data.path;
      showToast('Folder selected. Click Scan Folder to continue.', 'success');
    } else {
      showToast('No folder selected.', 'info');
    }
  } catch (err) {
    showToast('Browse error: ' + err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = _BROWSE_ICON + ' Browse';
  }
}

async function cleanerScan() {
  const dir     = cleanerNormPath(document.getElementById('cleaner-dir-input').value);
  const scanBtn = document.getElementById('btn-cleaner-scan');
  const delBtn  = document.getElementById('btn-cleaner-delete');
  const results = document.getElementById('cleaner-results');

  if (!dir) { showToast('Please enter a folder path first.'); return; }

  scanBtn.disabled = true;
  scanBtn.textContent = 'Scanning…';
  results.classList.add('hidden');
  delBtn.disabled = true;
  _cleanerLastFiles = [];

  try {
    const res  = await authFetch('/api/clean/run-files', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ directory: dir, dry_run: true }),
    });
    const data = await res.json();
    if (!res.ok) { showToast(data.detail || 'Scan failed', 'error'); return; }

    _cleanerLastFiles = data.files || [];
    cleanerShowResults(data.count, null, [], data.total_bytes);

    if (data.count > 0) delBtn.disabled = false;

  } catch (err) {
    showToast('Scan error: ' + err.message, 'error');
  } finally {
    scanBtn.disabled = false;
    scanBtn.textContent = 'Scan Folder';
  }
}

async function cleanerDelete() {
  if (!_cleanerLastFiles.length) return;

  const dir    = cleanerNormPath(document.getElementById('cleaner-dir-input').value);
  const delBtn = document.getElementById('btn-cleaner-delete');
  const scanBtn = document.getElementById('btn-cleaner-scan');

  // Confirm before deleting
  const confirmed = confirm(
    `Delete ${_cleanerLastFiles.length} run file(s) from:\n${dir}\n\nThis cannot be undone.`
  );
  if (!confirmed) return;

  delBtn.disabled  = true;
  scanBtn.disabled = true;
  delBtn.textContent = 'Deleting…';

  try {
    const res  = await authFetch('/api/clean/run-files', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ directory: dir, dry_run: false }),
    });
    const data = await res.json();
    if (!res.ok) { showToast(data.detail || 'Delete failed', 'error'); return; }

    _cleanerLastFiles = data.files || [];
    cleanerShowResults(data.count, data.deleted, data.errors, data.total_bytes);

    delBtn.disabled = true;   // no more files to delete
    showToast(`Deleted ${data.deleted} file(s) — ${_fmtBytes(data.total_bytes)} freed.`, 'success');

  } catch (err) {
    showToast('Delete error: ' + err.message, 'error');
  } finally {
    delBtn.textContent = 'Delete Files';
    scanBtn.disabled = false;
  }
}

function cleanerShowResults(count, deleted, errors = [], totalBytes = 0) {
  const results  = document.getElementById('cleaner-results');
  const summary  = document.getElementById('cleaner-results-summary');
  const fileList = document.getElementById('cleaner-file-list');
  const toggleBtn = document.getElementById('btn-cleaner-toggle-list');

  results.classList.remove('hidden');

  // Summary text
  if (deleted === null) {
    // Scan mode
    if (count === 0) {
      summary.textContent = '✓ No run files found — folder is clean.';
      summary.className = 'cleaner-results-summary cleaner-clean';
      toggleBtn.classList.add('hidden');
    } else {
      summary.textContent = `Found ${count} run file${count !== 1 ? 's' : ''} ready for deletion — ${_fmtBytes(totalBytes)}.`;
      summary.className = 'cleaner-results-summary cleaner-found';
      toggleBtn.classList.remove('hidden');
      toggleBtn.textContent = 'Show files';
    }
  } else {
    // Delete mode
    const errCount = (errors || []).length;
    summary.textContent = `Deleted ${deleted} of ${count} file${count !== 1 ? 's' : ''} — ${_fmtBytes(totalBytes)} freed.`
      + (errCount ? `  (${errCount} error${errCount !== 1 ? 's' : ''})` : '');
    summary.className = deleted === count
      ? 'cleaner-results-summary cleaner-clean'
      : 'cleaner-results-summary cleaner-found';
    toggleBtn.classList.remove('hidden');
    toggleBtn.textContent = 'Show files';
  }

  // File list
  fileList.innerHTML = '';
  fileList.classList.add('hidden');
  if (_cleanerLastFiles.length) {
    _cleanerLastFiles.forEach(fp => {
      const row = document.createElement('div');
      row.className = 'cleaner-file-row';
      // Show just the filename + parent folder for readability
      const parts = fp.replace(/\\/g, '/').split('/');
      const name  = parts.pop();
      const parent = parts.slice(-1)[0] || '';
      row.innerHTML =
        `<span class="cleaner-file-name">${name}</span>` +
        `<span class="cleaner-file-parent">${parent ? '…/' + parent : ''}</span>`;
      row.title = fp;
      fileList.appendChild(row);
    });
  }
}

// ─────────────────────────────────────────────────────────────────
//  P-M-M INTERACTION DIAGRAM MODULE
// ─────────────────────────────────────────────────────────────────

let _pmmResult  = null;   // last computed result from backend
let _pmmPayload = null;   // last request payload (for re-rendering)
let _pmmLoads   = [];     // [{id, label, P, Mx, My, ...checkResult}]
let _pmmLoadId  = 0;      // auto-increment id
let _pmmLoadsInited = false;
let _pmmSortCol = null;   // 'label'|'P'|'Mx'|'My'|'DCR'
let _pmmSortDir = 1;      // 1=asc, -1=desc

function pmmInitRebarTable() {
  authFetch('/api/pmm/rebar-table?units=SI')
    .then(r => r.ok ? r.json() : null)
    .then(data => {
      if (!data) return;
      const sel = document.getElementById('pmm-barsize');
      if (!sel) return;
      sel.innerHTML = '';
      Object.entries(data).forEach(([name, area]) => {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = `${name}  (${area} mm²)`;
        if (name === 'Ø20') opt.selected = true;
        sel.appendChild(opt);
      });
      pmmUpdateRhoInfo();
    })
    .catch(() => {});
}

function pmmInit() {
  pmmInitRebarTable();

  // Wire tab buttons
  document.querySelectorAll('.pmm-tab').forEach(btn => {
    btn.addEventListener('click', () => pmmShowTab(btn.dataset.tab));
  });

  // Wire Generate button
  document.getElementById('btn-pmm-generate')
    ?.addEventListener('click', pmmGenerate);

  // Wire Import from ETABS button
  document.getElementById('btn-pmm-etabs')
    ?.addEventListener('click', pmmImportETABS);

  // Live rho preview + section drawing on input change
  ['pmm-b','pmm-h','pmm-cover','pmm-stirrup-dia','pmm-nbars-b','pmm-nbars-h','pmm-barsize'].forEach(id => {
    document.getElementById(id)?.addEventListener('input', () => {
      pmmUpdateRhoInfo();
      pmmDrawSection();
    });
  });
  pmmUpdateRhoInfo();
  pmmDrawSection();

  // Always initialise the loads panel immediately (not tied to 3D tab click)
  pmmLoadsInit();
}

function pmmUpdateRhoInfo() {
  const b          = parseFloat(document.getElementById('pmm-b')?.value)          || 0;
  const h          = parseFloat(document.getElementById('pmm-h')?.value)          || 0;
  const clearCover = parseFloat(document.getElementById('pmm-cover')?.value)      || 0;
  const stirrupDia = parseFloat(document.getElementById('pmm-stirrup-dia')?.value)|| 10;
  const nbarsB = parseInt(document.getElementById('pmm-nbars-b')?.value)    || 0;
  const nbarsH = parseInt(document.getElementById('pmm-nbars-h')?.value)    || 0;
  const n      = 2 * nbarsB + 2 * nbarsH;  // total bars
  const barSel = document.getElementById('pmm-barsize');
  const barVal = barSel?.value || '';                        // e.g. "Ø20"
  const areaText = barSel?.selectedOptions[0]?.textContent || '';
  const areaMatch = areaText.match(/\(([0-9.]+)\s*m/);
  const ab    = areaMatch ? parseFloat(areaMatch[1]) : 0;
  // Extract longitudinal bar diameter from size label e.g. "Ø20" → 20 mm
  const barDia = parseFloat(barVal.replace(/[^0-9.]/g, '')) || 0;
  const info  = document.getElementById('pmm-rho-info');
  if (!info) return;

  if (b > 0 && h > 0 && n > 0 && ab > 0) {
    const Ag  = b * h;
    const Ast = n * ab;
    const rho = (Ast / Ag * 100).toFixed(2);
    const ok  = Ast / Ag >= 0.01 && Ast / Ag <= 0.08;
    // Effective cover to longitudinal bar centre
    const effCover = clearCover + stirrupDia + barDia / 2;
    // Effective depth to tension steel (minor axis d uses b, major axis d uses h)
    const dH = h - effCover;   // depth to bar centre for h-direction bending
    const dB = b - effCover;   // depth to bar centre for b-direction bending
    info.innerHTML =
      `${n} bars · ρ = ${rho}%  (ACI 318: 1–8%)<br>` +
      `<span class="pmm-rho-d">` +
        `Stirrup Ø${stirrupDia}  ·  eff. cover = ${effCover.toFixed(0)} mm  ·  ` +
        `d<sub>h</sub> = ${dH.toFixed(0)} mm  ·  d<sub>b</sub> = ${dB.toFixed(0)} mm` +
      `</span>`;
    info.className = 'pmm-rho-info' + (ok ? '' : ' pmm-rho-warn');
  } else {
    info.textContent = '';
  }
}

async function pmmGenerate() {
  const btn = document.getElementById('btn-pmm-generate');
  const b     = parseFloat(document.getElementById('pmm-b').value);
  const h     = parseFloat(document.getElementById('pmm-h').value);
  const fc    = parseFloat(document.getElementById('pmm-fc').value);
  const fy    = parseFloat(document.getElementById('pmm-fy').value);
  const es    = parseFloat(document.getElementById('pmm-es').value);
  const cover      = parseFloat(document.getElementById('pmm-cover').value);
  const stirrupDia = parseFloat(document.getElementById('pmm-stirrup-dia')?.value) || 10;
  const nbarsB = parseInt(document.getElementById('pmm-nbars-b').value);
  const nbarsH = parseInt(document.getElementById('pmm-nbars-h').value) || 0;
  const barSel  = document.getElementById('pmm-barsize');
  const barSize = barSel?.value || 'Ø20';
  const phi   = document.getElementById('pmm-phi')?.checked ?? true;
  const resSel = document.getElementById('pmm-res');
  const [alphaSteps, numPoints] = (resSel?.value || '10:70').split(':').map(Number);

  if (!b || !h || !fc || !fy || !es || !cover || !nbarsB) {
    pmmSetStatus('Please fill all required fields.', 'error'); return;
  }
  if (cover >= Math.min(b, h) / 2) {
    pmmSetStatus('Cover is too large relative to section size.', 'error'); return;
  }

  btn.disabled   = true;
  btn.textContent = 'Computing…';
  pmmSetStatus('', '');

  const payload = {
    b, h, fc, fy, es,
    cover, stirrup_dia_mm: stirrupDia,
    nbars_b: nbarsB, nbars_h: nbarsH, bar_size: barSize,
    include_phi: phi,
    alpha_steps: alphaSteps,
    num_points:  numPoints,
    units: 'SI',
    demand_P:   parseFloat(document.getElementById('pmm-dp')?.value)  || null,
    demand_Mx:  parseFloat(document.getElementById('pmm-dmx')?.value) || null,
    demand_My:  parseFloat(document.getElementById('pmm-dmy')?.value) || null,
  };

  try {
    const res2 = await authFetch('/api/pmm/calculate', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(payload),
    });
    const data = await res2.json();
    if (!res2.ok) throw new Error(data.detail || 'Calculation failed');

    _pmmResult  = data;
    _pmmPayload = payload;
    // Log engine version so we can verify the correct pmm_engine is loaded
    console.log('[PMM] engine_version =', data.engine_version || 'not-reported');
    // Clear stale check results when a new diagram is generated
    _pmmLoads.forEach(l => { delete l.DCR; delete l.status; delete l.M_cap; delete l.M_demand; });
    pmmShowSummary(data);
    pmmRender3D(data, payload);
    pmmRender2D(data, 'pmx', 'P–Mx', 'Mx', payload);
    pmmRender2D(data, 'pmy', 'P–My', 'My', payload);
    pmmShowTab('3d');
    document.getElementById('pmm-chart-empty')?.classList.add('hidden');
  } catch(e) {
    pmmSetStatus(e.message, 'error');
  } finally {
    btn.disabled   = false;
    btn.textContent = 'Generate Diagram';
  }
}

function pmmShowSummary(data) {
  document.getElementById('pmm-summary')?.classList.remove('hidden');
  document.getElementById('pmm-s-ag').textContent   = `${data.Ag} mm²`;
  document.getElementById('pmm-s-ast').textContent  = `${data.Ast} mm²`;
  document.getElementById('pmm-s-rho').textContent  = `${data.rho}%`;
  document.getElementById('pmm-s-pmax').textContent = `${data.Pmax} kN`;
  document.getElementById('pmm-s-pmin').textContent = `${data.Pmin} kN`;
}

function pmmSetStatus(msg, type) {
  const el = document.getElementById('pmm-status');
  if (!el) return;
  el.textContent = msg;
  el.className   = 'pmm-status' + (type === 'error' ? ' pmm-status-error' : '');
}

function pmmShowTab(tab) {
  // 3D tab uses the wrapper (chart + loads panel)
  const wrapper3d = document.getElementById('pmm-3d-wrapper');
  const show3d = tab === '3d';
  wrapper3d?.classList.toggle('hidden', !show3d);
  document.querySelector('.pmm-tab[data-tab="3d"]')?.classList.toggle('active', show3d);
  if (show3d) {
    pmmLoadsInit();
    if (_pmmResult) {
      try { Plotly.Plots.resize(document.getElementById('pmm-chart-3d')); } catch(e) {}
    }
  }

  // 2D tabs (P-Mx, P-My) — toggle the outer wrapper (button + chart)
  ['pmx','pmy'].forEach(t => {
    const outerEl = document.getElementById(`pmm-2d-outer-${t}`);
    const chartEl = document.getElementById(`pmm-chart-${t}`);
    const tabBtn  = document.querySelector(`.pmm-tab[data-tab="${t}"]`);
    const show    = t === tab;
    outerEl?.classList.toggle('hidden', !show);
    tabBtn?.classList.toggle('active', show);
    if (show && _pmmResult && chartEl) {
      try { Plotly.Plots.resize(chartEl); } catch(e) {}
    }
  });

  // Mx-My tab — renders when clicked (never auto-triggered by load checks)
  const mxmyEl      = document.getElementById('pmm-chart-mxmy');
  const mxmyToolbar = document.getElementById('pmm-mxmy-toolbar');
  const mxmyBtn     = document.querySelector('.pmm-tab[data-tab="mxmy"]');
  const showMxMy    = tab === 'mxmy';
  mxmyEl?.classList.toggle('hidden', !showMxMy);
  mxmyToolbar?.classList.toggle('hidden', !showMxMy);
  mxmyBtn?.classList.toggle('active', showMxMy);

  // Batch Results tab
  const batchPanel = document.getElementById('pmm-batch-results-panel');
  const batchBtn   = document.querySelector('.pmm-tab[data-tab="batch"]');
  const showBatch  = tab === 'batch';
  batchPanel?.classList.toggle('hidden', !showBatch);
  batchBtn?.classList.toggle('active', showBatch);
  if (showMxMy) pmmPopulateMxMyPDropdown();
  if (showMxMy && _pmmResult) {
    const checkedLoads = _pmmLoads.filter(l => l.status);
    const autoEngineP  = checkedLoads.length ? -(+checkedLoads[0].P)
                       : (_pmmLoads.length && _pmmLoads[0].P !== '' ? -(+_pmmLoads[0].P)
                         : (_pmmResult ? _pmmResult.Pmax * 0.35 : 0));
    // Pre-fill input with the auto-detected P (user-facing: negative = compression)
    const pInput = document.getElementById('pmm-mxmy-p-input');
    if (pInput && pInput.value === '') pInput.value = (-autoEngineP).toFixed(1);
    const userP   = pInput && pInput.value !== '' ? parseFloat(pInput.value) : -autoEngineP;
    const Ptarget = isNaN(userP) ? autoEngineP : -userP;
    const loadPts = checkedLoads.length ? checkedLoads : _pmmLoads.filter(l => l.P !== '');
    pmmRenderMxMy(_pmmResult, _pmmPayload, Ptarget, loadPts);
    try { Plotly.Plots.resize(mxmyEl); } catch(e) {}
  }
}

function pmmMxMyUpdateP(val) {
  if (!_pmmResult) return;
  const userP = parseFloat(val);
  if (isNaN(userP)) return;
  const Ptarget = -userP;  // engine-sign: positive = compression
  const loadPts = _pmmLoads.filter(l => l.P !== '');
  pmmRenderMxMy(_pmmResult, _pmmPayload, Ptarget, loadPts);
}

let _pmmMxMyPSaved = null;

// Dropdown arrow button — clear input so datalist shows all options
function pmmMxMyOpenDrop(e) {
  e.preventDefault();
  const input = document.getElementById('pmm-mxmy-p-input');
  _pmmMxMyPSaved = input.value;
  input.value = '';
  input.focus();
}

// Restore saved value if user dismisses without selecting
function pmmMxMyPBlur() {
  const input = document.getElementById('pmm-mxmy-p-input');
  if (input.value === '' && _pmmMxMyPSaved !== null) {
    input.value = _pmmMxMyPSaved;
  }
  _pmmMxMyPSaved = null;
}

// Step through demand P values (dir: +1 = next higher, -1 = next lower)
function pmmMxMyStepP(e, dir) {
  e.preventDefault();
  const input = document.getElementById('pmm-mxmy-p-input');
  const cur = parseFloat(input.value);
  const vals = [...new Set(_pmmLoads.filter(l => l.P !== '').map(l => +l.P))].sort((a, b) => a - b);
  let newVal;
  if (vals.length === 0) {
    newVal = (isNaN(cur) ? 0 : cur) + dir * 100;
  } else if (isNaN(cur)) {
    newVal = dir > 0 ? vals[vals.length - 1] : vals[0];
  } else if (dir > 0) {
    const next = vals.find(v => v > cur + 0.05);
    newVal = next !== undefined ? next : vals[vals.length - 1];
  } else {
    const prev = [...vals].reverse().find(v => v < cur - 0.05);
    newVal = prev !== undefined ? prev : vals[0];
  }
  input.value = newVal.toFixed(1);
  pmmMxMyUpdateP(input.value);
}

function pmmPopulateMxMyPDropdown() {
  const dl = document.getElementById('pmm-mxmy-p-list');
  if (!dl) return;
  const loads = _pmmLoads.filter(l => l.P !== '' && l.P != null);
  // Group labels by unique P value
  const map = new Map();
  loads.forEach(l => {
    const key = (+l.P).toFixed(1);
    if (!map.has(key)) map.set(key, []);
    if (l.label) map.get(key).push(l.label);
  });
  // Sort ascending (most negative = highest compression first)
  const sorted = [...map.entries()].sort((a, b) => +a[0] - +b[0]);
  dl.innerHTML = sorted.map(([p, labels]) => {
    const desc = labels.length ? ` — ${labels.slice(0, 3).join(', ')}${labels.length > 3 ? '…' : ''}` : '';
    return `<option value="${p}">${p} kN${desc}</option>`;
  }).join('');
}

function pmmDrawSection() {
  const svg = document.getElementById('pmm-section-svg');
  if (!svg) return;

  const b          = parseFloat(document.getElementById('pmm-b')?.value)          || 0;
  const h          = parseFloat(document.getElementById('pmm-h')?.value)          || 0;
  const clearCover = parseFloat(document.getElementById('pmm-cover')?.value)      || 0;
  const stirrupDia = parseFloat(document.getElementById('pmm-stirrup-dia')?.value)|| 10;
  const nbarsB = Math.max(2, parseInt(document.getElementById('pmm-nbars-b')?.value) || 2);
  const nbarsH = Math.max(0, parseInt(document.getElementById('pmm-nbars-h')?.value) || 0);
  const barSel = document.getElementById('pmm-barsize');
  const barVal = barSel?.value || '';
  const areaText = barSel?.selectedOptions[0]?.textContent || '';
  const areaMatch = areaText.match(/\(([0-9.]+)\s*m/);
  const ab = areaMatch ? parseFloat(areaMatch[1]) : 0;
  const barR = ab > 0 ? Math.sqrt(ab / Math.PI) : 0;  // bar radius in mm
  // Effective cover = clear cover + stirrup diameter + longitudinal bar radius
  const barDia   = parseFloat(barVal.replace(/[^0-9.]/g, '')) || (barR * 2);
  const effCover = clearCover + stirrupDia + barDia / 2;
  // Stirrup centreline offset from face
  const stCover  = clearCover + stirrupDia / 2;

  if (b <= 0 || h <= 0) { svg.innerHTML = ''; return; }

  // Canvas dimensions with margins
  const W = 200, H = 200, pad = 14;
  const scale = Math.min((W - 2 * pad) / b, (H - 2 * pad) / h);
  const ox = (W - b * scale) / 2;   // origin x
  const oy = (H - h * scale) / 2;   // origin y (top)

  const px = x => ox + x * scale;
  const py = y => oy + (h - y) * scale;   // flip y (SVG y grows down)

  // Bar positions using EFFECTIVE cover (clear cover + stirrup dia + bar radius)
  const bars = [];
  const x0 = effCover, x1 = b - effCover, y0 = effCover, y1 = h - effCover;
  const bLen = Math.max(0, x1 - x0), hLen = Math.max(0, y1 - y0);

  // Bottom face
  for (let i = 0; i < nbarsB; i++) {
    const x = nbarsB > 1 ? x0 + bLen * i / (nbarsB - 1) : (x0 + x1) / 2;
    bars.push([x, y0]);
  }
  // Right face (intermediate only)
  for (let j = 0; j < nbarsH; j++) {
    bars.push([x1, y0 + hLen * (j + 1) / (nbarsH + 1)]);
  }
  // Top face
  for (let i = nbarsB - 1; i >= 0; i--) {
    const x = nbarsB > 1 ? x0 + bLen * i / (nbarsB - 1) : (x0 + x1) / 2;
    bars.push([x, y1]);
  }
  // Left face (intermediate only)
  for (let j = nbarsH - 1; j >= 0; j--) {
    bars.push([x0, y0 + hLen * (j + 1) / (nbarsH + 1)]);
  }

  const barRpx  = Math.max(2.5, Math.min(barR * scale, 6));
  const stDiaPx = Math.max(1, stirrupDia * scale);          // stirrup thickness px
  const total   = 2 * nbarsB + 2 * nbarsH;
  // Stirrup centreline rectangle coordinates
  const sx0 = stCover, sx1 = b - stCover, sy0 = stCover, sy1 = h - stCover;

  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.innerHTML = `
    <!-- Section outline -->
    <rect x="${px(0)}" y="${py(h)}" width="${b * scale}" height="${h * scale}"
          fill="#e8f0fe" stroke="#2563eb" stroke-width="1.5" rx="1"/>
    <!-- Stirrup outline (centreline, orange) -->
    <rect x="${px(sx0)}" y="${py(sy1)}" width="${(sx1-sx0)*scale}" height="${(sy1-sy0)*scale}"
          fill="none" stroke="#f59e0b" stroke-width="${Math.max(1, stDiaPx)}" rx="1"/>
    <!-- Bar centreline boundary (dashed, faint) -->
    <rect x="${px(x0)}" y="${py(y1)}" width="${bLen * scale}" height="${hLen * scale}"
          fill="none" stroke="#93c5fd" stroke-width="0.7" stroke-dasharray="3,2"/>
    <!-- Longitudinal bars -->
    ${bars.map(([bx, by]) =>
      `<circle cx="${px(bx)}" cy="${py(by)}" r="${barRpx}"
               fill="#1e5a8a" stroke="#fff" stroke-width="0.8"/>`
    ).join('')}
    <!-- Dimension labels -->
    <text x="${W / 2}" y="${py(h) - 4}" text-anchor="middle"
          font-size="9" fill="#475569" font-family="sans-serif">b = ${b} mm</text>
    <text x="${px(0) - 4}" y="${H / 2}" text-anchor="middle"
          font-size="9" fill="#475569" font-family="sans-serif"
          transform="rotate(-90,${px(0) - 4},${H / 2})">h = ${h} mm</text>
    <!-- Bar count -->
    <text x="${W / 2}" y="${py(0) + 11}" text-anchor="middle"
          font-size="8" fill="#64748b" font-family="sans-serif">${total} bars</text>
  `;
}

async function pmmImportETABS() {
  const btn = document.getElementById('btn-pmm-etabs');
  if (btn) { btn.disabled = true; btn.textContent = 'Loading…'; }
  pmmSetStatus('', '');

  try {
    const res = await authFetch('/api/pmm/etabs-sections');
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || 'ETABS not connected');
    }
    const data = await res.json();
    const sections = data.sections || [];
    if (!sections.length) {
      pmmSetStatus('No RC column sections found in ETABS model.', 'error');
      return;
    }

    // If only one section, populate directly
    if (sections.length === 1) {
      pmmFillFromSection(sections[0]);
      pmmSetStatus(`Imported: ${sections[0].name || sections[0].prop_name || ''}`, '');
      return;
    }

    // Multiple sections: show a picker modal
    pmmShowSectionPicker(sections);
  } catch (e) {
    pmmSetStatus(e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Import from ETABS'; }
  }
}

async function pmmFillFromSection(sec, comboOverride = null) {
  // sec: { name/prop_name, b_mm/width, h_mm/depth, cover_mm/cover,
  //         nbars, rebar_size (label like "Ø20"), fc_mpa, fy_mpa, Es_mpa }
  // Normalise field aliases so both old (prop_name/width/depth/cover) and
  // new (name/b_mm/h_mm/cover_mm) naming conventions work.
  const b      = sec.b_mm     ?? sec.width;
  const h      = sec.h_mm     ?? sec.depth;
  const cover  = sec.cover_mm ?? sec.cover;
  const setVal = (id, v) => {
    const el = document.getElementById(id);
    if (el && v != null) el.value = v;
  };
  setVal('pmm-b',     Math.round(b));
  setVal('pmm-h',     Math.round(h));
  setVal('pmm-cover', Math.round(cover));
  // nbars_b = top/bottom (b-dir) face bars INCLUDING corners → pmm-nbars-b
  // nbars_h = side (h-dir) INTERMEDIATE bars EXCLUDING corners → pmm-nbars-h
  // ETABS: vals[6]=nbars_2 = Along 3-dir face = top/bottom → nbars_b
  //        vals[7]=nbars_3 = Along 2-dir face = side face  → nbars_h = nbars_3 - 2
  const nb_b = sec.nbars_b ?? sec.nbars_2;
  const nb_h = sec.nbars_h ?? (sec.nbars_3 != null ? Math.max(0, sec.nbars_3 - 2) : null);
  if (nb_b != null) setVal('pmm-nbars-b', nb_b);
  if (nb_h != null) setVal('pmm-nbars-h', nb_h);
  if (sec.fc_mpa  != null) setVal('pmm-fc', parseFloat(sec.fc_mpa.toFixed(1)));
  if (sec.fy_mpa  != null) setVal('pmm-fy', parseFloat(sec.fy_mpa.toFixed(0)));
  if (sec.Es_mpa  != null) setVal('pmm-es', parseFloat(sec.Es_mpa.toFixed(0)));

  // Try to match bar size in select
  const barSel = document.getElementById('pmm-barsize');
  if (barSel && sec.rebar_size) {
    const label = String(sec.rebar_size); // e.g. "Ø20" or diameter number
    const matchOpt = [...barSel.options].find(o =>
      o.value === label || o.value === `Ø${label}`
    );
    if (matchOpt) barSel.value = matchOpt.value;
  }

  pmmUpdateRhoInfo();
  pmmDrawSection();

  // ── Auto-import demand forces for this section from ETABS ──────────────
  const propName = sec.prop_name || sec.name;
  if (!propName) return;
  const note = document.getElementById('pmm-loads-note');
  try {
    // Use provided combos, or fetch all available combos
    let comboNames = comboOverride;
    if (!comboNames) {
      const combosRes = await authFetch('/api/pmm/etabs-combos');
      if (!combosRes.ok) return;
      const combosData = await combosRes.json();
      comboNames = (combosData.combos || []).map(c => (typeof c === 'string' ? c : c.name || String(c)));
    }
    if (!comboNames.length) return;

    const forcesRes = await authFetch('/api/pmm/etabs-section-forces', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ section_name: propName, combo_names: comboNames })
    });
    if (!forcesRes.ok) return;
    const forcesData = await forcesRes.json();
    const rows = forcesData.results || [];
    if (!rows.length) return;

    // Populate demand table: match batch engine convention → Mx = M33, My = M22
    _pmmLoads = [];
    _pmmLoadId = 0;
    rows.forEach(r => {
      _pmmLoadId++;
      _pmmLoads.push({ id: _pmmLoadId, label: r.label,
                       P: r.P_kN, Mx: r.M3_kNm, My: r.M2_kNm });
    });
    pmmRenderLoadsRows();
    pmmPopulateMxMyPDropdown();
    if (note) note.textContent = `✓ ${rows.length} demand load(s) auto-imported for "${propName}".`;
  } catch { /* silently skip if ETABS not connected or section has no frames */ }
}

function pmmShowSectionPicker(sections) {
  // Remove existing picker if any
  document.getElementById('pmm-section-picker')?.remove();

  const overlay = document.createElement('div');
  overlay.id = 'pmm-section-picker';
  overlay.style.cssText = `
    position:fixed;inset:0;z-index:9999;background:rgba(15,23,42,0.55);
    display:flex;align-items:center;justify-content:center;`;

  const box = document.createElement('div');
  box.style.cssText = `
    background:var(--bg-card);border-radius:10px;padding:20px;min-width:320px;
    max-width:460px;max-height:70vh;overflow-y:auto;box-shadow:0 8px 32px rgba(0,0,0,0.18);`;

  const title = document.createElement('div');
  title.textContent = 'Select ETABS Section';
  title.style.cssText = 'font-weight:600;font-size:14px;color:var(--t1);margin-bottom:12px;';
  box.appendChild(title);

  sections.forEach(sec => {
    const row = document.createElement('button');
    row.style.cssText = `
      display:block;width:100%;text-align:left;padding:9px 12px;margin-bottom:6px;
      border:1px solid var(--bdr-sm);border-radius:6px;background:var(--bg-input);
      color:var(--t1);font-size:13px;cursor:pointer;line-height:1.4;`;
    const _label = sec.name || sec.prop_name || '(unnamed)';
    const _b     = sec.b_mm  ?? sec.width  ?? '?';
    const _h     = sec.h_mm  ?? sec.depth  ?? '?';
    // nbars_b = top/bottom (b-dir) bars incl. corners; nbars_h = side intermediate only.
    // PMM total = 2*nbars_b + 2*nbars_h.
    // Fallback: nbars_2=Along-3dir-face = top/bottom; nbars_3=Along-2dir-face = side(incl corners).
    const _nb_b = +(sec.nbars_b ?? sec.nbars_2 ?? 0);
    const _nb_h_raw = sec.nbars_h ?? (sec.nbars_3 != null ? Math.max(0, sec.nbars_3 - 2) : 0);
    const _nb_h = +(_nb_h_raw ?? 0);
    const _nb   = (sec.nbars != null) ? sec.nbars
                : (_nb_b) ? 2 * _nb_b + 2 * _nb_h : '—';
    row.innerHTML = `<strong>${_label}</strong><br>
      <span style="color:var(--t2);font-size:12px">
        ${Math.round(_b)}×${Math.round(_h)} mm  |  ${_nb} bars  |
        f'c ${sec.fc_mpa?.toFixed(0) ?? '—'} MPa  |  fy ${sec.fy_mpa?.toFixed(0) ?? '—'} MPa
      </span>`;
    row.addEventListener('click', () => {
      overlay.remove();
      pmmFillFromSection(sec);
      pmmSetStatus(`Imported: ${sec.name || sec.prop_name || ''}`, '');
    });
    row.addEventListener('mouseenter', () => row.style.background = 'var(--blue-tint)');
    row.addEventListener('mouseleave', () => row.style.background = 'var(--bg-input)');
    box.appendChild(row);
  });

  const cancel = document.createElement('button');
  cancel.textContent = 'Cancel';
  cancel.style.cssText = `
    margin-top:8px;padding:7px 16px;border:1px solid var(--bdr-sm);
    border-radius:6px;background:transparent;color:var(--t2);cursor:pointer;font-size:13px;`;
  cancel.addEventListener('click', () => overlay.remove());
  box.appendChild(cancel);

  overlay.appendChild(box);
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
  document.body.appendChild(overlay);
}

function pmmRender3D(data, payload, loadPts) {
  const el = document.getElementById('pmm-chart-3d');
  if (!el) return;

  const surf = data.surface;
  const allMx = surf.Mx, allMy = surf.My, allP = surf.P;
  // After bar-transition clustering the engine may return more points per
  // meridian than payload.num_points.  Use surf.num_points when available.
  const numPts   = surf.num_points || payload.num_points;
  const numAlpha = Math.round(allP.length / numPts); // number of alpha angles
  const arrMax = (a) => a.reduce((m, v) => v > m ? v : m, -Infinity);
  const arrMin = (a) => a.reduce((m, v) => v < m ? v : m,  Infinity);
  const ext = Math.max(arrMax(allMx.map(Math.abs)), arrMax(allMy.map(Math.abs))) * 1.15;

  // ── Build per-meridian reference arrays ────────────────────────────────
  // The engine now returns a *uniform global P grid* for every meridian
  // (all alphas share the same P value at each index k, after the engine's
  // global-P-grid surface rebuild + outer-envelope pass).
  // rMx/rMy/rP are therefore simply views into allMx/allMy/allP — but we
  // keep a small outer-envelope safety scan so older engine responses or
  // edge cases (residual numerical drift) are handled gracefully.
  const Pglo_min = arrMin(allP);
  const Pglo_max = arrMax(allP);
  const rMx = new Array(numAlpha * numPts);
  const rMy = new Array(numAlpha * numPts);
  const rP  = new Array(numAlpha * numPts);
  for (let a = 0; a < numAlpha; a++) {
    const base = a * numPts;
    const mP  = allP .slice(base, base + numPts);
    const mMx = allMx.slice(base, base + numPts);
    const mMy = allMy.slice(base, base + numPts);
    // Meridian P bounds — used to zero-out queries above/below this meridian's range
    const mPmin = Math.min(...mP);
    const mPmax = Math.max(...mP);
    for (let k = 0; k < numPts; k++) {
      const Pt = Pglo_min + (Pglo_max - Pglo_min) * k / (numPts - 1);
      // If Pt is above this meridian's maximum P, the section cannot carry any
      // moment at that compression level → return zero instead of the stale
      // last-point fallback that caused the outward spike artefact.
      if (Pt > mPmax + 1e-6) {
        rMx[base + k] = 0; rMy[base + k] = 0; rP[base + k] = Pt;
        continue;
      }
      // Fast path: engine provides monotonic P → direct index copy
      // Safety: full scan in case of residual non-monotonicity
      let mx = 0, my = 0, bestM = -1;
      for (let j = 0; j < numPts - 1; j++) {
        const p1 = mP[j], p2 = mP[j + 1];
        const dp = p2 - p1;
        if (Math.abs(dp) < 1e-12) continue;
        const t = (Pt - p1) / dp;
        if (t < -1e-9 || t > 1 + 1e-9) continue;
        const tc  = Math.max(0, Math.min(1, t));
        const cMx = mMx[j] + tc * (mMx[j + 1] - mMx[j]);
        const cMy = mMy[j] + tc * (mMy[j + 1] - mMy[j]);
        const M   = cMx * cMx + cMy * cMy;
        if (M > bestM) { bestM = M; mx = cMx; my = cMy; }
      }
      if (bestM < 0) {
        // Only use the low-P fallback (pure tension end); never extrapolate upward
        mx = Pt <= mPmin ? mMx[0] : 0;
        my = Pt <= mPmin ? mMy[0] : 0;
      }
      rMx[base + k] = mx;
      rMy[base + k] = my;
      rP [base + k] = Pt;
    }
  }

  // ── Angular super-sampling (4×): insert 3 interpolated meridians between ──
  // each adjacent pair.  With typical 5° engine sweeps (72 real meridians)
  // this gives 288 effective meridians → 1.25° apparent angular resolution,
  // matching the density spColumn uses for its smooth 3-D surface.
  const SUPER      = 4;
  const numAlphaS  = numAlpha * SUPER;
  const nVerts     = numAlphaS * numPts;
  const sMx = new Array(nVerts);
  const sMy = new Array(nVerts);
  const sP  = new Array(nVerts);
  for (let a = 0; a < numAlpha; a++) {
    const a1 = (a + 1) % numAlpha;
    for (let s = 0; s < SUPER; s++) {
      const t  = s / SUPER;
      const sa = a * SUPER + s;
      for (let k = 0; k < numPts; k++) {
        const b0 = a  * numPts + k;
        const b1 = a1 * numPts + k;
        sMx[sa * numPts + k] = rMx[b0] * (1 - t) + rMx[b1] * t;
        sMy[sa * numPts + k] = rMy[b0] * (1 - t) + rMy[b1] * t;
        sP [sa * numPts + k] = rP[b0];   // P identical for same k across all alphas
      }
    }
  }

  // ── Per-ring convex hull smoothing ─────────────────────────────────────────
  // Each horizontal k-ring is the Mx-My cross-section of the surface at one P
  // level.  The ACI φ-factor nose (TC→CC switching) creates concavities: some
  // meridians at mid-P levels pick the tension-controlled branch (φ=0.90, higher
  // M) while adjacent meridians only have the CC branch (φ=0.65, lower M).
  // This causes the visible waist indentations on the 3-D surface.
  //
  // Fix: apply the support-function convex hull to EACH ring independently with
  // 4-fold symmetry enforcement.  For each alpha direction the hull picks the
  // supersampled ring point that projects furthest in that direction.  This
  // makes every P-ring perfectly convex, matching spColumn's smooth surface.
  for (let k = 0; k < numPts; k++) {
    // Build 4-fold symmetric cloud from the current ring
    const rx = [], ry = [];
    for (let a = 0; a < numAlphaS; a++) {
      const mx = sMx[a * numPts + k];
      const my = sMy[a * numPts + k];
      rx.push( mx,  mx, -mx, -mx);
      ry.push( my, -my,  my, -my);
    }
    // Support function: for each alpha direction keep the furthest-projecting pt
    for (let a = 0; a < numAlphaS; a++) {
      const ang = (a / numAlphaS) * 2 * Math.PI;
      const dx = Math.cos(ang), dy = Math.sin(ang);
      let bestDot = -1e30, bx = 0, by = 0;
      for (let j = 0; j < rx.length; j++) {
        const dot = dx * rx[j] + dy * ry[j];
        if (dot > bestDot) { bestDot = dot; bx = rx[j]; by = ry[j]; }
      }
      sMx[a * numPts + k] = bx;
      sMy[a * numPts + k] = by;
    }
  }

  const traces = [];

  // ── Find topK (last ring with non-zero moment) BEFORE triangulating ──────
  // The zero-moment rings beyond topK form a degenerate "stem" up to Pmax.
  // Clipping quads at topK and closing with a FLAT DISC at P_topK gives the
  // correct ACI 318 flat-cap appearance — no spike/cone at the top.
  let topK = numPts - 1;
  let mMax = 1;
  for (let i = 0; i < sMx.length; i++) {
    const v = Math.abs(sMx[i]) > Math.abs(sMy[i]) ? Math.abs(sMx[i]) : Math.abs(sMy[i]);
    if (v > mMax) mMax = v;
  }
  const mThresh = 0.03 * mMax;   // 3% of peak moment — clips degenerate near-Pmax
                                  // rings while keeping the cap seam invisible

  // ── Scan topK (from top downward) ────────────────────────────────────────
  for (let k = numPts - 1; k >= 0; k--) {
    let anyNonZero = false;
    for (let a = 0; a < numAlphaS; a++) {
      const idx = a * numPts + k;
      if (Math.abs(sMx[idx]) > mThresh || Math.abs(sMy[idx]) > mThresh) {
        anyNonZero = true; break;
      }
    }
    if (anyNonZero) { topK = k; break; }
  }

  // ── Zero-force the k=0 ring to the origin ────────────────────────────────
  // At pure axial tension (Pglo_min) every meridian has M=0 (symmetric section,
  // no eccentricity).  Numerical drift in the resampling can leave tiny non-zero
  // values that produce the concave "heart" artefact at the bottom tip.
  // Forcing (Mx,My)=0 at k=0 makes the bottom converge cleanly to a point;
  // no separate disc-cap is needed — the quad triangles at i=0 naturally form
  // a fan that tapers to the origin (degenerate triangles are silently ignored
  // by Plotly's mesh3d renderer).
  for (let a = 0; a < numAlphaS; a++) {
    sMx[a * numPts] = 0;
    sMy[a * numPts] = 0;
  }

  // ── Triangulate quads from k=0 to topK ───────────────────────────────────
  const triI = [], triJ = [], triK = [];
  for (let a = 0; a < numAlphaS; a++) {
    const a1 = (a + 1) % numAlphaS;
    for (let i = 0; i < topK; i++) {
      const p00 = a  * numPts + i;
      const p10 = a1 * numPts + i;
      const p01 = a  * numPts + i + 1;
      const p11 = a1 * numPts + i + 1;
      triI.push(p00); triJ.push(p10); triK.push(p01);
      triI.push(p10); triJ.push(p11); triK.push(p01);
    }
  }
  // Normalise P [0=max-tension … 1=max-compression] for smooth colour gradient
  const Prange    = (Pglo_max - Pglo_min) || 1;
  const intensity = sP.map(p => (p - Pglo_min) / Prange);

  // ── Flat top cap: horizontal disc at P_topK ───────────────────────────────
  const P_topK       = sP[topK];
  const topCenterIdx = sMx.length;
  sMx.push(0); sMy.push(0); sP.push(P_topK);
  intensity.push((P_topK - Pglo_min) / Prange);
  for (let a = 0; a < numAlphaS; a++) {
    const a1 = (a + 1) % numAlphaS;
    triI.push(topCenterIdx);
    triJ.push(a  * numPts + topK);
    triK.push(a1 * numPts + topK);
  }
  // No separate bottom cap needed — the zeroed k=0 ring already closes the tip.

  traces.push({
    type: 'mesh3d',
    x: sMx, y: sMy, z: sP,
    i: triI, j: triJ, k: triK,
    // Uniform light-steel-blue matching the spColumn reference — no gradient.
    // Glossy finish: high specular + low roughness + strong fresnel edge sheen.
    color: '#b8cce4',
    showscale: false,
    opacity: 0.30,
    hoverinfo: 'none',
    name: 'Surface',
    flatshading: false,
    lighting: {
      ambient:   0.55,   // lower ambient lets shadows deepen the gloss effect
      diffuse:   0.50,   // moderate diffuse for smooth shading across the surface
      specular:  0.85,   // high specular → bright sharp highlight on the curve
      roughness: 0.08,   // near-zero roughness = mirror-like glossy sheen
      fresnel:   0.30,   // strong fresnel → glowing silhouette / rim highlight
    },
    lightposition: { x: 1000, y: 500, z: 8000 },
  });

  // ── Wireframe: meridian lines ─────────────────────────────────────────────
  // Always show ~12 evenly-spaced meridians regardless of angular resolution.
  // Using rMx/rMy (uniform-P resampled) so each line lies exactly on the
  // smooth surface rather than on the raw pre-envelope data.
  // Clipped at topK to avoid the convergent spike at the compression cap.
  // Draw meridians from hull-smoothed sMx/sMy (original alpha steps × SUPER).
  const numMeridians = 12;
  const mStep = Math.max(1, Math.round(numAlphaS / numMeridians));
  const mxM = [], myM = [], pM = [];
  for (let a = 0; a < numAlphaS; a += mStep) {
    const base = a * numPts;
    for (let i = 0; i <= topK; i++) {
      mxM.push(sMx[base + i]);
      myM.push(sMy[base + i]);
      pM.push(sP [base + i]);
    }
    mxM.push(null); myM.push(null); pM.push(null);
  }
  // Meridian wireframe hidden — vertical lines removed from 3D surface
  // traces.push({
  //   type: 'scatter3d', mode: 'lines',
  //   x: mxM, y: myM, z: pM,
  //   line: { color: '#7aa8c8', width: 1.0 },
  //   hoverinfo: 'none',
  //   name: 'Meridians',
  //   showlegend: false,
  // });

  // ── Find balanced condition: ring index where total moment is maximum ──────
  // ACI 318-19 balanced condition = highest moment on the outer envelope.
  // Used to draw a highlighted ring that marks this key ACI 318 point.
  let balIdx = 0, balMmax = 0;
  for (let k = 0; k < numPts; k++) {
    let ringM = 0;
    for (let a = 0; a < numAlpha; a++) {
      const idx = a * numPts + k;
      const m = Math.sqrt(allMx[idx] * allMx[idx] + allMy[idx] * allMy[idx]);
      if (m > ringM) ringM = m;
    }
    if (ringM > balMmax) { balMmax = ringM; balIdx = k; }
  }
  const Pbalanced = rP[balIdx];  // factored P at balanced condition

  // ── Wireframe: horizontal ring lines (true constant-P-level cuts) ──────
  // Use the resampled (rMx/rMy/rP) data so each ring connects points at the
  // SAME P level across all alpha angles → perfectly planar, smooth ellipses.
  // Clip rings at topK so we don't draw degenerate zero-moment rings
  // in the Pn,max plateau — those rings are just points and create cluttered
  // wireframe near the apex.  The filled mesh covers that region.
  // Ring wireframe: use full numAlphaS resolution with hull-smoothed values
  // so rings are perfectly convex ellipses on the smooth surface.
  const numRings = 15;
  const rStep = Math.max(1, Math.floor(numPts / numRings));
  const mxR = [], myR = [], pR = [];
  for (let k = 0; k < numPts; k += rStep) {
    if (k > topK)     continue;  // skip degenerate cap rings at top
    if (k === balIdx) continue;  // drawn separately as highlighted ring
    for (let a = 0; a < numAlphaS; a++) {
      mxR.push(sMx[a * numPts + k]);
      myR.push(sMy[a * numPts + k]);
      pR.push(sP [a * numPts + k]);
    }
    mxR.push(sMx[0 * numPts + k]); myR.push(sMy[0 * numPts + k]); pR.push(sP[0 * numPts + k]); // close
    mxR.push(null); myR.push(null); pR.push(null);
  }
  traces.push({
    type: 'scatter3d', mode: 'lines',
    x: mxR, y: myR, z: pR,
    line: { color: '#93c5fd', width: 0.8 },
    hoverinfo: 'none',
    name: 'Rings',
    showlegend: false,
  });

  // ── Balanced condition ring (highlighted, ACI 318 key feature) ─────────
  // This ring marks the maximum moment capacity (balanced condition per ACI 318-19).
  // The P-M-M surface is widest at this ring.
  {
    const mxB = [], myB = [], pB = [];
    for (let a = 0; a < numAlphaS; a++) {
      mxB.push(sMx[a * numPts + balIdx]);
      myB.push(sMy[a * numPts + balIdx]);
      pB.push(sP [a * numPts + balIdx]);
    }
    mxB.push(sMx[0 * numPts + balIdx]); myB.push(sMy[0 * numPts + balIdx]); pB.push(sP[0 * numPts + balIdx]);
    traces.push({
      type: 'scatter3d', mode: 'lines',
      x: mxB, y: myB, z: pB,
      line: { color: '#f59e0b', width: 3 },
      hovertemplate: `Balanced Condition<br>φP = ${Pbalanced.toFixed(0)} kN<br>φM<sub>max</sub> = ${balMmax.toFixed(0)} kN·m<extra></extra>`,
      name: 'Balanced',
      showlegend: false,
    });
  }

  // ── ACI 318 key-level labels (φPn,max / balanced / pure tension) ───────
  const lblMx = ext * 0.85;
  traces.push({
    type: 'scatter3d', mode: 'text',
    x: [lblMx, lblMx, lblMx],
    y: [0,     0,     0    ],
    z: [Pglo_max,  Pbalanced, Pglo_min],
    text: [
      `φPn,max = ${Pglo_max.toFixed(0)} kN`,
      `Balanced  φP = ${Pbalanced.toFixed(0)} kN`,
      `φTn = ${Pglo_min.toFixed(0)} kN`,
    ],
    textposition: 'middle right',
    textfont: { color: ['#1e40af', '#b45309', '#1d4ed8'], size: 10 },
    hoverinfo: 'none',
    showlegend: false,
  });

  // ── P = 0 horizontal reference plane ────────────────────────────
  traces.push({
    type: 'mesh3d',
    x: [-ext,  ext,  ext, -ext],
    y: [-ext, -ext,  ext,  ext],
    z: [   0,    0,    0,    0],
    i: [0, 0], j: [1, 2], k: [2, 3],
    color: '#22c55e',
    opacity: 0.10,
    showscale: false,
    hoverinfo: 'none',
    name: 'P = 0',
  });

  // ── Load demand points — DCR-coloured 3-D spheres ────────────────────────
  // 6-band flat colorscale matching reference image:
  //   cyan(0-20%) | lime(20-40%) | yellow(40-60%) | orange(60-80%) | pink(80-100%) | red(>100%)
  if (loadPts && loadPts.length) {
    // cmax=120 so all 6 bands are exactly 20 units wide (equal length in legend):
    //   0-20 cyan | 20-40 lime | 40-60 yellow | 60-80 orange | 80-100 pink | 100-120 red
    // Sharp hard edges achieved by repeating stop colours at each boundary (t-ε / t).
    const dcrColorscale = [
      [0.0000, '#00e5ff'],  // cyan        — 0 %
      [0.1666, '#00e5ff'],  // cyan        — 20 %  (20/120)
      [0.1667, '#00dd00'],  // lime green  — 20 %
      [0.3332, '#00dd00'],  // lime green  — 40 %
      [0.3333, '#ffff00'],  // yellow      — 40 %
      [0.4999, '#ffff00'],  // yellow      — 60 %
      [0.5000, '#ff8c00'],  // orange      — 60 %
      [0.6665, '#ff8c00'],  // orange      — 80 %
      [0.6666, '#ff007f'],  // hot pink    — 80 %
      [0.8332, '#ff007f'],  // hot pink    — 100 %
      [0.8333, '#cc0000'],  // dark red    — 100 %
      [1.0000, '#cc0000'],  // dark red    — >100 %
    ];

    // Tick labels at band centres (each band = 20 units wide, cmax=120)
    const dcrTickVals = [10, 30, 50, 70, 90, 110];
    const dcrTickText = ['20%', '40%', '60%', '80%', '100%', '>100%'];

    traces.push({
      type: 'scatter3d', mode: 'markers',
      x: loadPts.map(p => +p.Mx),
      y: loadPts.map(p => +p.My),
      z: loadPts.map(p => -(+p.P)),
      text: loadPts.map(p => p.label || 'Load'),
      marker: {
        // Size scales with DCR: base 14px, grows to 32px at 100%+ DCR
        size: loadPts.map(p => {
          const pct = Math.min(Math.max((parseFloat(p.DCR) || 0) * 100, 0), 109);
          return 14 + 18 * Math.sqrt(pct / 109);   // 14 at 0%, ~23 at 50%, 32 at 109%
        }),
        symbol: 'circle',
        color: loadPts.map(p => Math.min(Math.max((parseFloat(p.DCR) || 0) * 100, 0), 119)),
        colorscale: dcrColorscale,
        cmin: 0,
        cmax: 120,
        showscale: true,
        colorbar: {
          title: {
            text: 'DCR (%)',
            side: 'right',
            font: { size: 12, color: '#1e293b' },
          },
          thickness: 20,
          len: 0.75,
          x: 1.02,
          xanchor: 'left',
          tickvals: dcrTickVals,
          ticktext: dcrTickText,
          tickfont: { size: 11, color: '#1e293b' },
          outlinecolor: '#94a3b8',
          outlinewidth: 1,
        },
        // Glossy 3D sphere look: semi-transparent + bright white highlight ring
        opacity: 0.85,
        line: {
          color: 'rgba(255,255,255,0.90)',  // bright white rim = gloss highlight
          width: 1.5,
        },
      },
      name: 'Loads',
      hovertemplate: loadPts.map(p =>
        `<b>${p.label||'Load'}</b><br>` +
        `P: ${(+(p.P)||0).toFixed(1)} kN<br>` +
        `Mx: ${(+(p.Mx)||0).toFixed(1)} kN·m<br>` +
        `My: ${(+(p.My)||0).toFixed(1)} kN·m<br>` +
        `DCR: ${((parseFloat(p.DCR)||0)*100).toFixed(1)}%` +
        `<extra></extra>`
      ),
    });
  } else if (payload.demand_P != null && payload.demand_Mx != null && payload.demand_My != null) {
    traces.push({
      type: 'scatter3d', mode: 'markers',
      x: [payload.demand_Mx], y: [payload.demand_My], z: [payload.demand_P],
      marker: { size: 8, color: '#ef4444', symbol: 'circle', opacity: 0.9 },
      name: 'Demand',
      hovertemplate: `Demand<br>P: ${payload.demand_P} kN<br>Mx: ${payload.demand_Mx} kN·m<br>My: ${payload.demand_My} kN·m<extra></extra>`,
    });
  }

  // ── Smart P-axis scaling ──────────────────────────────────────────
  // Collect full range: surface P + any demand load P/Mx/My (engine-sign)
  let PsmartMin = arrMin(allP), PsmartMax = arrMax(allP);
  let mExt = ext;  // moment axis extent — start from surface extent
  if (loadPts && loadPts.length) {
    loadPts.forEach(p => {
      const pz = -(+p.P);   // engine-sign (positive = compression)
      if (pz < PsmartMin) PsmartMin = pz;
      if (pz > PsmartMax) PsmartMax = pz;
      const mx = Math.abs(+p.Mx), my = Math.abs(+p.My);
      if (mx > mExt) mExt = mx;
      if (my > mExt) mExt = my;
    });
    mExt *= 1.1;  // 10% breathing room
  }
  const Pspan  = PsmartMax - PsmartMin || 1;
  // Pick a "nice" tick interval ≈ Pspan / 5
  const rawStep  = Pspan / 5;
  const stepExp  = Math.pow(10, Math.floor(Math.log10(rawStep)));
  const stepFrac = rawStep / stepExp;
  const Pdtick   = stepFrac < 1.5 ? stepExp
                 : stepFrac < 3   ? 2 * stepExp
                 : stepFrac < 7   ? 5 * stepExp
                 :                 10 * stepExp;
  // Snap axis bounds outward to next tick, add one extra tick of breathing room
  const PaxMin = (Math.floor(PsmartMin / Pdtick) - 1) * Pdtick;
  const PaxMax = (Math.ceil (PsmartMax / Pdtick) + 1) * Pdtick;

  // ── Smart aspect ratio: compress tall P axis so surface isn't a spike ──
  // Compare visible P span vs Mx/My span; clamp result to [0.5, 1.5]
  const mSpan    = 2 * mExt;          // full Mx/My width
  const pSpanVis = PaxMax - PaxMin;   // visible P height
  const zAspect  = Math.min(Math.max(pSpanVis / mSpan, 0.5), 1.5);

  const layout = {
    paper_bgcolor: '#ffffff',
    plot_bgcolor:  '#ffffff',
    margin: { l: 0, r: 80, t: 36, b: 0 },  // right margin for DCR colorbar
    scene: {
      bgcolor: '#ffffff',
      aspectmode: 'manual',
      aspectratio: { x: 1, y: 1, z: zAspect },
      xaxis: { title: 'Mx (kN·m)', color: '#475569', gridcolor: '#cbd5e1', zeroline: true, zerolinecolor: '#94a3b8', range: [-mExt, mExt] },
      yaxis: { title: 'My (kN·m)', color: '#475569', gridcolor: '#cbd5e1', zeroline: true, zerolinecolor: '#94a3b8', range: [-mExt, mExt] },
      zaxis: {
        title: 'P (kN)', color: '#475569', gridcolor: '#cbd5e1',
        zeroline: true, zerolinecolor: '#94a3b8',
        range: [PaxMin, PaxMax],
        dtick: Pdtick,
        tick0: 0,
      },
      camera: { eye: { x: 1.6, y: 1.6, z: 1.1 } },
    },
    showlegend: false,
    title: { text: 'P-M-M Interaction Surface', font: { color: '#0f172a', size: 13 }, x: 0.5 },
  };

  Plotly.react(el, traces, layout, { responsive: true, displayModeBar: false });
}

const _pmm3DViews = {
  '3d':    { eye: { x: 1.6,   y: 1.6,   z: 1.1   }, up: { x:0, y:0, z:1 } },
  'top':   { eye: { x: 0.001, y: 0.001, z: 2.5   }, up: { x:0, y:1, z:0 } },
  'front': { eye: { x: 0.001, y: 2.5,   z: 0.001 }, up: { x:0, y:0, z:1 } },
  'side':  { eye: { x: 2.5,   y: 0.001, z: 0.001 }, up: { x:0, y:0, z:1 } },
};

function pmmSet3DView(view) {
  const el = document.getElementById('pmm-chart-3d');
  if (!el || !el._fullLayout) return;
  const cam = _pmm3DViews[view];
  if (!cam) return;
  Plotly.relayout(el, { 'scene.camera': { eye: cam.eye, up: cam.up, center: {x:0,y:0,z:0} } });
  document.querySelectorAll('.pmm-3d-vbtn[data-view]').forEach(b => b.classList.toggle('active', b.dataset.view === view));
}

let _pmm3DRotateTimer = null;
let _pmm3DRotateAngle = 0;

function pmmToggleRotate() {
  const btn = document.getElementById('pmm-3d-rotate-btn');
  if (_pmm3DRotateTimer) {
    clearInterval(_pmm3DRotateTimer);
    _pmm3DRotateTimer = null;
    if (btn) { btn.textContent = '▶'; btn.classList.remove('active'); }
    return;
  }
  if (btn) { btn.textContent = '⏹'; btn.classList.add('active'); }
  const el = document.getElementById('pmm-chart-3d');
  if (!el || !el._fullLayout) return;
  const R = 2.0;   // eye radius
  const Z = 1.1;   // eye z height
  _pmm3DRotateTimer = setInterval(() => {
    _pmm3DRotateAngle = (_pmm3DRotateAngle + 1.5) % 360;
    const rad = _pmm3DRotateAngle * Math.PI / 180;
    Plotly.relayout(el, {
      'scene.camera': {
        eye: { x: R * Math.cos(rad), y: R * Math.sin(rad), z: Z },
        up:  { x: 0, y: 0, z: 1 },
        center: { x: 0, y: 0, z: 0 }
      }
    });
  }, 40);  // ~25 fps
}

function pmmRender2D(data, chartId, title, momentKey, payload, loadPts) {
  const el = document.getElementById(`pmm-chart-${chartId}`);
  if (!el) return;

  const c2d    = data.curves_2d;
  const traces = [];
  const palette = { '0': '#3b82f6', '90': '#22c55e', '180': '#f59e0b', '270': '#ef4444' };
  const labels  = { '0': 'α=0°', '90': 'α=90°', '180': 'α=180°', '270': 'α=270°' };

  // Mx (about X-axis) = horizontal NA → α=0°/180°; My (about Y-axis) = vertical NA → α=90°/270°.
  // Only show the relevant meridians so orthogonal (near-zero) sweeps don't pollute the chart.
  const relevantAngles = momentKey === 'My' ? new Set(['90', '270']) : new Set(['0', '180']);

  Object.entries(c2d).forEach(([angle, curve]) => {
    if (!relevantAngles.has(angle)) return;   // skip orthogonal sweeps
    const mArr = momentKey === 'Mx' ? curve.Mx : curve.My;

    // Find balanced condition (max |M|) for this meridian
    let balI = 0, balMval = 0;
    mArr.forEach((m, i) => { if (Math.abs(m) > balMval) { balMval = Math.abs(m); balI = i; } });
    const Pbal = curve.P[balI], Mbal = mArr[balI];

    traces.push({
      type: 'scatter', mode: 'lines',
      x: mArr, y: curve.P,
      line: { color: palette[angle] || '#888', width: 2, shape: 'spline', smoothing: 1.3 },
      name: labels[angle] || `α=${angle}°`,
      hovertemplate: `${momentKey}: %{x:.1f} kN·m<br>P: %{y:.1f} kN<extra></extra>`,
    });

    // Mark balanced condition with a diamond marker on this meridian
    traces.push({
      type: 'scatter', mode: 'markers',
      x: [Mbal], y: [Pbal],
      marker: { size: 9, symbol: 'diamond', color: '#f59e0b',
                line: { color: '#92400e', width: 1.5 } },
      name: `Balanced (${labels[angle] || `α=${angle}°`})`,
      hovertemplate: `Balanced condition<br>${momentKey}: ${Mbal.toFixed(1)} kN·m<br>P: ${Pbal.toFixed(1)} kN<extra></extra>`,
      showlegend: false,
    });
  });

  // Load demand markers
  if (loadPts && loadPts.length) {
    const pass = loadPts.filter(p => p.status === 'PASS');
    const fail = loadPts.filter(p => p.status === 'FAIL');

    // Helper: biaxial capacity component on the plotted moment axis
    const capComp = p => momentKey === 'Mx' ? p._capMx : p._capMy;
    const demComp = p => momentKey === 'Mx' ? p.Mx : p.My;
    // Build hover text showing full biaxial context
    const hoverText = p =>
      `${p.label||'Load'}<br>` +
      `P: ${(+(p.P)||0).toFixed(1)} kN<br>` +
      `Mx: ${(+(p.Mx)||0).toFixed(1)} kN·m  My: ${(+(p.My)||0).toFixed(1)} kN·m<br>` +
      `M_d: ${p.M_demand != null ? (+p.M_demand).toFixed(1) : '–'} kN·m  ` +
      `M_cap: ${p.M_cap != null ? (+p.M_cap).toFixed(1) : '–'} kN·m  ` +
      `DCR: ${p.DCR != null ? (+p.DCR).toFixed(3) : '–'}`;

    if (pass.length) traces.push({
      type: 'scatter', mode: 'markers',
      x: pass.map(demComp), y: pass.map(p => -(+p.P)),
      marker: { size: 9, color: '#16a34a', symbol: 'circle',
                line: { color: '#fff', width: 1 } },
      name: 'PASS',
      hovertemplate: `%{text}<extra></extra>`,
      text: pass.map(hoverText),
    });
    if (fail.length) traces.push({
      type: 'scatter', mode: 'markers',
      x: fail.map(demComp), y: fail.map(p => -(+p.P)),
      marker: { size: 10, color: '#dc2626', symbol: 'x',
                line: { color: '#dc2626', width: 2 } },
      name: 'FAIL',
      hovertemplate: `%{text}<extra></extra>`,
      text: fail.map(hoverText),
    });
  } else {
    // Single-demand fallback (no loadPts): demand_P is in user sign (compression = negative),
    // convert to engine sign (positive = compression) to match the capacity curves.
    const dM = momentKey === 'Mx' ? payload.demand_Mx : payload.demand_My;
    if (payload.demand_P != null && dM != null) {
      traces.push({
        type: 'scatter', mode: 'markers',
        x: [dM], y: [-(+payload.demand_P)],
        marker: { size: 10, color: '#fff', symbol: 'diamond',
                  line: { color: '#3b82f6', width: 2 } },
        name: 'Demand',
        hovertemplate: `Demand<br>${momentKey}: ${dM} kN·m<br>P: ${payload.demand_P} kN<extra></extra>`,
      });
    }
  }

  const layout = {
    paper_bgcolor: '#ffffff',
    plot_bgcolor:  '#f8fafc',
    margin: { l: 56, r: 20, t: 36, b: 50 },
    xaxis: {
      title: { text: `${momentKey} (kN·m)`, font: { color: '#475569' } },
      color: '#475569', gridcolor: '#e2e8f0', zeroline: true, zerolinecolor: '#94a3b8',
    },
    yaxis: {
      title: { text: 'P (kN)', font: { color: '#475569' } },
      color: '#475569', gridcolor: '#e2e8f0', zeroline: true, zerolinecolor: '#94a3b8',
    },
    legend: { font: { color: '#475569' }, bgcolor: 'rgba(255,255,255,0.8)' },
    title: { text: title, font: { color: '#0f172a', size: 13 }, x: 0.5 },
  };

  Plotly.react(el, traces, layout, { responsive: true, displayModeBar: false });
}

/**
 * Export the 2D P–Mx or P–My interaction curve as a CSV file.
 * Format is compatible with spColumn (P in kN compression-positive, M in kN·m).
 */
function pmmExport2DCSV(chartId, momentKey) {
  if (!_pmmResult) { alert('Generate the PMM diagram first.'); return; }

  const c2d    = _pmmResult.curves_2d;
  const angles = momentKey === 'My' ? ['90', '270'] : ['0', '180'];
  const rows   = [];

  // ── File header ─────────────────────────────────────────────────────────────
  rows.push(`# StructIQ – P–${momentKey} Interaction Curve`);
  rows.push(`# Units  : P in kN (positive = compression), ${momentKey} in kN·m`);
  rows.push(`# Columns: alpha_deg, P_kN, ${momentKey}_kNm`);
  rows.push(`alpha_deg,P_kN,${momentKey}_kNm`);

  // ── Interaction curve points ─────────────────────────────────────────────────
  angles.forEach(angle => {
    const curve = c2d[angle];
    if (!curve) return;
    const mArr = momentKey === 'Mx' ? curve.Mx : curve.My;
    for (let i = 0; i < curve.P.length; i++) {
      rows.push(`${angle},${curve.P[i].toFixed(2)},${mArr[i].toFixed(3)}`);
    }
  });

  // ── Demand points (if checked) ───────────────────────────────────────────────
  const checked = _pmmLoads.filter(l => l.P !== '' && l.status);
  if (checked.length) {
    rows.push('');
    rows.push(`# Demand loads`);
    rows.push(`label,P_kN,${momentKey}_kNm,DCR,status`);
    checked.forEach(d => {
      const Peng = -(+d.P);                                   // engine sign: +compression
      const m    = +(momentKey === 'Mx' ? d.Mx : d.My) || 0;
      const dcr  = d.DCR != null ? (+d.DCR).toFixed(3) : '';
      rows.push(`${d.label},${Peng.toFixed(2)},${m.toFixed(3)},${dcr},${d.status || ''}`);
    });
  }

  const blob = new Blob([rows.join('\r\n')], { type: 'text/csv;charset=utf-8;' });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href = url;  a.download = `StructIQ_P${momentKey}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// ── PMM boundary helpers (shared by Mx-My chart and DCR update) ──────────────

/** Compute the Mx-My boundary polygon at the given Ptarget (engine-sign kN). */
/**
 * Compute the Mx-My capacity boundary at a given axial load Ptarget.
 *
 * Returns a 360-point parametric ellipse { bndMx, bndMy } centred on the
 * origin with semi-axes Mx_max (pure-Mx capacity) and My_max (pure-My
 * capacity) at this P level.
 *
 * Why an ellipse instead of the raw convex-hull polygon?
 *   With only 36 angle meridians (10° steps) the raw hull is a visible
 *   36-gon, and — critically — at P levels where some meridians exceed their
 *   individual Pmax, those meridians return (0,0), producing a "spike" hull
 *   that is NOT a closed convex boundary surrounding the origin.  The ray-
 *   intersection solver then finds near-zero intersections, giving wildly
 *   incorrect (and unconservative) DCR values.
 *
 *   The parametric ellipse is ALWAYS a proper closed boundary.  It perfectly
 *   matches spColumn's Mx-My display (spColumn renders the same smooth ellipse
 *   from 36 angle samples), and the ray intersection is exact:
 *     t = 1 / sqrt( (dx/Mx_max)² + (dy/My_max)² )
 *     → cap = (t·dx , t·dy)
 */
function pmmBoundaryAtP(data, payload, Ptarget) {
  const surf = data.surface;
  const allMx = surf.Mx, allMy = surf.My, allP = surf.P;
  const numPts   = surf.num_points || payload.num_points;
  const numAlpha = Math.round(allP.length / numPts);

  // Step 1: Interpolate (Mx, My) at Ptarget for each meridian — same logic as
  // pmmRender3D so the Mx-My slice exactly matches the 3D surface cross-section.
  const rawMx = [], rawMy = [];
  for (let a = 0; a < numAlpha; a++) {
    const base = a * numPts;
    const mP  = allP .slice(base, base + numPts);
    const mMx = allMx.slice(base, base + numPts);
    const mMy = allMy.slice(base, base + numPts);
    const mPmax = Math.max(...mP);
    const mPmin = Math.min(...mP);
    if (Ptarget > mPmax + 1e-6) { rawMx.push(0); rawMy.push(0); continue; }
    let mx = 0, my = 0, bestM = -1;
    for (let j = 0; j < numPts - 1; j++) {
      const p1 = mP[j], p2 = mP[j + 1];
      const dp = p2 - p1;
      if (Math.abs(dp) < 1e-12) continue;
      const t = (Ptarget - p1) / dp;
      if (t < -1e-9 || t > 1 + 1e-9) continue;
      const tc  = Math.max(0, Math.min(1, t));
      const cMx = mMx[j] + tc * (mMx[j + 1] - mMx[j]);
      const cMy = mMy[j] + tc * (mMy[j + 1] - mMy[j]);
      const M   = cMx * cMx + cMy * cMy;
      if (M > bestM) { bestM = M; mx = cMx; my = cMy; }
    }
    if (bestM < 0) {
      mx = Ptarget <= mPmin ? mMx[0] : 0;
      my = Ptarget <= mPmin ? mMy[0] : 0;
    }
    rawMx.push(mx);
    rawMy.push(my);
  }

  // Step 2: Build 4-fold symmetric cloud (mirrors all quadrants) — same as
  // the per-ring hull in pmmRender3D so both views share the same geometry.
  const rx = [], ry = [];
  for (let a = 0; a < numAlpha; a++) {
    const mx = rawMx[a], my = rawMy[a];
    rx.push( mx,  mx, -mx, -mx);
    ry.push( my, -my,  my, -my);
  }

  // Step 3: Support-function hull — for each output direction pick the point
  // that projects furthest, giving a perfectly convex closed boundary that
  // matches the 3D surface cross-section shape (superellipse for rectangular
  // columns, not a perfect ellipse).
  const N_OUT = 360;
  const bndMx = [], bndMy = [];
  let Mx_max = 0, My_max = 0;
  for (let i = 0; i <= N_OUT; i++) {
    const ang = (i / N_OUT) * 2 * Math.PI;
    const dx = Math.cos(ang), dy = Math.sin(ang);
    let bestDot = -1e30, bx = 0, by = 0;
    for (let j = 0; j < rx.length; j++) {
      const dot = dx * rx[j] + dy * ry[j];
      if (dot > bestDot) { bestDot = dot; bx = rx[j]; by = ry[j]; }
    }
    bndMx.push(bx);
    bndMy.push(by);
    if (Math.abs(bx) > Mx_max) Mx_max = Math.abs(bx);
    if (Math.abs(by) > My_max) My_max = Math.abs(by);
  }

  return { bndMx, bndMy, Mx_max, My_max };
}

/** Ray from origin in direction (dx, dy) intersected with boundary polygon. */
function pmmRayBoundaryIntersect(dx, dy, bx, by) {
  const n = bx.length - 1;
  let bestT = Infinity, rx = null, ry = null;
  for (let i = 0; i < n; i++) {
    const Ax = bx[i], Ay = by[i], Bx = bx[i+1], By = by[i+1];
    const dBx = Bx - Ax, dBy = By - Ay;
    const det = dx * dBy - dy * dBx;
    if (Math.abs(det) < 1e-10) continue;
    const t = (Ax * dBy - Ay * dBx) / det;
    const s = (Ax * dy  - Ay * dx ) / det;
    if (t > 1e-9 && s >= -1e-9 && s <= 1 + 1e-9 && t < bestT) {
      bestT = t; rx = t * dx; ry = t * dy;
    }
  }
  return rx !== null ? { x: rx, y: ry } : null;
}

/**
 * For each load in `loads`, compute boundary-based M_cap / DCR using the
 * boundary at that demand's own P level, then update p.M_cap, p.DCR, p.status.
 */
function pmmUpdateDCRFromBoundary(loads) {
  if (!_pmmResult || !_pmmPayload) return;
  loads.forEach(p => {
    // Ray direction matches display space — no swap needed.
    const mx = +p.Mx, my = +p.My;
    const Md = p.M_demand != null ? +p.M_demand : Math.sqrt(mx*mx + my*my);
    if (Md < 1e-9) return;
    const Ptarget = -(+p.P);   // engine-sign: positive = compression
    const { bndMx, bndMy } = pmmBoundaryAtP(_pmmResult, _pmmPayload, Ptarget);
    const cap = pmmRayBoundaryIntersect(mx, my, bndMx, bndMy);
    if (!cap) return;
    const M_geo = Math.sqrt(cap.x*cap.x + cap.y*cap.y);
    if (M_geo < 1e-9) return;
    p.M_cap  = +M_geo.toFixed(3);
    p.DCR    = +(Md / M_geo).toFixed(3);
    p.status = p.DCR <= 1.0 ? 'PASS' : 'FAIL';
    // Store biaxial cap components in display convention
    p._capMx = +cap.x.toFixed(3);
    p._capMy = +cap.y.toFixed(3);
  });
}

// ── PMM Mx-My slice chart ─────────────────────────────────────────────────────
function pmmRenderMxMy(data, payload, Ptarget, loadPts) {
  const el = document.getElementById('pmm-chart-mxmy');
  if (!el) return;

  // pmmBoundaryAtP now returns the 360-point parametric ellipse directly.
  // bndMx/bndMy is used for BOTH display and DCR ray-intersection — consistent.
  const { bndMx, bndMy, Mx_max, My_max } = pmmBoundaryAtP(data, payload, Ptarget);

  const traces = [];

  // Near Pn,max both axes → 0; show a single dot at origin.
  const M_eps = 0.01;
  const dispMx = (Mx_max < M_eps && My_max < M_eps) ? [0] : bndMx;
  const dispMy = (Mx_max < M_eps && My_max < M_eps) ? [0] : bndMy;

  traces.push({
    type: 'scatter', mode: 'lines',
    x: dispMx, y: dispMy,
    fill: 'toself', fillcolor: 'rgba(59,130,246,0.12)',
    line: { color: '#3b82f6', width: 2, shape: 'linear' },
    name: 'Capacity',
    hovertemplate: 'Mx: %{x:.1f} kN·m<br>My: %{y:.1f} kN·m<extra></extra>',
  });

  // Demand points — p.P is stored ETABS-sign (negative=compression); Ptarget is engine-sign (positive)
  // So compare -(+p.P) against Ptarget for the P-level filter
  if (loadPts && loadPts.length) {
    const tol = Math.max(50, Math.abs(Ptarget) * 0.15 + 50);
    const near = loadPts.filter(p => Math.abs(-(+p.P) - Ptarget) <= tol);
    if (near.length) {
      // Pass 1: intersect each demand with the displayed boundary at Ptarget → update M_cap/DCR/status.
      // Ray direction matches the demand star position in display space — no swap needed.
      near.forEach(p => {
        const mx = +p.Mx, my = +p.My;
        const Md = p.M_demand != null ? +p.M_demand : Math.sqrt(mx*mx + my*my);
        if (Md < 1e-9) return;
        const cap = pmmRayBoundaryIntersect(mx, my, bndMx, bndMy);
        if (!cap) return;
        const M_geo = Math.sqrt(cap.x*cap.x + cap.y*cap.y);
        if (M_geo < 1e-9) return;
        p._capX  = cap.x;
        p._capY  = cap.y;
        p.M_cap  = +M_geo.toFixed(3);
        p.DCR    = +(Md / M_geo).toFixed(3);
        p.status = p.DCR <= 1.0 ? 'PASS' : 'FAIL';
      });

      // Pass 2: draw radial projection lines using the updated geometric cap points.
      // capX/capY already in display convention; demand line uses display (p.Mx, p.My).
      near.forEach(p => {
        const mx = +p.Mx, my = +p.My;   // display: Mx=M22, My=M33
        const Md = p.M_demand != null ? +p.M_demand : 0;
        if (Md < 1e-6 || p._capX == null) return;
        const capMx = p._capX, capMy = p._capY;
        const M_geo = p.M_cap;
        const col = p.status !== 'FAIL' ? '#16a34a' : '#dc2626';
        // Dashed line: origin → capacity boundary
        traces.push({
          type: 'scatter', mode: 'lines',
          x: [0, capMx], y: [0, capMy],
          line: { color: col, width: 1.2, dash: 'dot' },
          showlegend: false, hoverinfo: 'skip',
        });
        // Solid line: origin → demand point
        traces.push({
          type: 'scatter', mode: 'lines',
          x: [0, mx], y: [0, my],
          line: { color: col, width: 2 },
          showlegend: false, hoverinfo: 'skip',
        });
        // Diamond marker at capacity boundary
        traces.push({
          type: 'scatter', mode: 'markers',
          x: [capMx], y: [capMy],
          marker: { symbol: 'diamond', size: 7, color: col,
                    line: { color: '#fff', width: 1 } },
          showlegend: false,
          hovertemplate: `M_cap (boundary): ${M_geo.toFixed(2)} kN·m<extra></extra>`,
        });
      });

      const pass = near.filter(p => p.status !== 'FAIL');
      const fail = near.filter(p => p.status === 'FAIL');
      [pass, fail].forEach((pts, fi) => {
        if (!pts.length) return;
        const col = fi ? '#dc2626' : '#16a34a';
        traces.push({
          type: 'scatter', mode: 'markers+text',
          x: pts.map(p => +p.Mx), y: pts.map(p => +p.My),
          text: pts.map(p => p.label || ''),
          textposition: 'top center',
          textfont: { size: 9, color: col },
          marker: { symbol: 'star', size: 10, color: col,
                    line: { color: '#fff', width: 1 } },
          name: fi ? 'FAIL' : 'PASS',
          hovertemplate: pts.map(p => {
            const dcr = p.DCR != null ? (parseFloat(p.DCR)*100).toFixed(1)+'%' : '–';
            const md  = p.M_demand != null ? (+p.M_demand).toFixed(2) : '–';
            const mc  = p.M_cap    != null ? (+p.M_cap   ).toFixed(2) : '–';
            return `${p.label||''}<br>Mx: ${(+p.Mx).toFixed(2)} kN·m<br>My: ${(+p.My).toFixed(2)} kN·m<br>M_d: ${md} kN·m<br>M_cap: ${mc} kN·m<br>DCR: ${dcr}<extra></extra>`;
          }),
        });
      });
    }
  }

  // Title shows user-facing P (negative = compression, matching ETABS convention)
  const Pdisplay = (-Ptarget).toFixed(1);
  const layout = {
    paper_bgcolor: '#ffffff', plot_bgcolor: '#f8fafc',
    margin: { l: 64, r: 40, t: 48, b: 64 },
    title: {
      text: `Mx–My Interaction  |  P = ${Pdisplay} kN`,
      font: { size: 13, color: '#1e293b' }, x: 0.5,
    },
    xaxis: {
      title: { text: 'Mx (kN·m)', font: { color: '#475569' } },
      color: '#475569', gridcolor: '#e2e8f0',
      zeroline: true, zerolinecolor: '#94a3b8', zerolinewidth: 1,
      scaleanchor: 'y', scaleratio: 1,
    },
    yaxis: {
      title: { text: 'My (kN·m)', font: { color: '#475569' } },
      color: '#475569', gridcolor: '#e2e8f0',
      zeroline: true, zerolinecolor: '#94a3b8', zerolinewidth: 1,
    },
    showlegend: true,
    legend: { x: 1, xanchor: 'right', y: 1, bgcolor: 'rgba(255,255,255,0.85)',
              bordercolor: '#e2e8f0', borderwidth: 1, font: { size: 11 } },
  };

  Plotly.react(el, traces, layout, { responsive: true, displayModeBar: false });
  // Refresh the loads table so DCR/status columns reflect the boundary-based values
  pmmRenderLoadsRows();
}

// ── PMM Loads Panel ──────────────────────────────────────────────────────────

function pmmLoadsInit() {
  if (_pmmLoadsInited) return;
  _pmmLoadsInited = true;
  const panel = document.getElementById('pmm-chart-loads');
  if (!panel) return;
  panel.innerHTML = `
    <div class="pmm-loads-header">
      <span class="pmm-loads-title">Demand Loads</span>
      <div style="display:flex;gap:6px;align-items:center">
        <button class="pmm-loads-btn-add" id="btn-add-row" onclick="pmmAddLoad()">+ Row</button>
        <button class="pmm-loads-btn-copy" onclick="pmmCopyLoadsToClipboard()" title="Copy table to clipboard (paste into Excel)">📋 Copy</button>
        <button class="pmm-loads-btn-export" onclick="pmmExportReport()" title="Export Word report">📄 Export</button>
        <button class="pmm-loads-btn-check" id="btn-pmm-check" onclick="pmmCheckLoads()">⚡ Check DCR</button>
      </div>
    </div>
    <div class="pmm-loads-tabs">
      <button class="pmm-loads-tab active" id="tab-manual" onclick="pmmSwitchTab('manual')">Manual</button>
      <button class="pmm-loads-tab" id="tab-etabs" onclick="pmmSwitchTab('etabs')">ETABS Import</button>
    </div>
    <div id="pmm-loads-manual-panel">
      <div class="pmm-loads-hint-row">— paste from Excel (Tab/Enter separated)</div>
      <div class="pmm-loads-table-wrap" id="pmm-loads-table-wrap">
        <table class="pmm-loads-table" id="pmm-loads-table">
          <thead>
            <tr>
              <th></th>
              <th class="col-label pmm-sort-th" onclick="pmmSortLoads('label')" id="sth-label">Label</th>
              <th class="col-num pmm-sort-th"   onclick="pmmSortLoads('P')"     id="sth-P">P<br><span class="th-unit">(kN)</span></th>
              <th class="col-num pmm-sort-th"   onclick="pmmSortLoads('Mx')"    id="sth-Mx">Mx<br><span class="th-unit">(kN·m)</span></th>
              <th class="col-num pmm-sort-th"   onclick="pmmSortLoads('My')"    id="sth-My">My<br><span class="th-unit">(kN·m)</span></th>
              <th class="col-res" id="sth-Md"  title="Total moment demand = √(Mx²+My²)">M<sub>d</sub><br><span class="th-unit">(kN·m)</span></th>
              <th class="col-res" id="sth-Mc"  title="Moment capacity at demand P-level and direction">M<sub>cap</sub><br><span class="th-unit">(kN·m)</span></th>
              <th class="col-res" id="sth-al"  title="Demand angle α = atan2(My,Mx)">α<br><span class="th-unit">(°)</span></th>
              <th class="col-dcr pmm-sort-th"   onclick="pmmSortLoads('DCR')"   id="sth-DCR">DCR</th>
              <th class="col-st">Status</th>
              <th class="col-del"></th>
            </tr>
            <tr class="pmm-filter-row">
              <th></th>
              <th><input class="pmm-filter-input" id="flt-label" type="text" placeholder="Filter…" oninput="pmmApplyLoadsFilter()" /></th>
              <th><input class="pmm-filter-input" id="flt-p"     type="number" placeholder="≥" step="any" oninput="pmmApplyLoadsFilter()" /></th>
              <th><input class="pmm-filter-input" id="flt-mx"    type="number" placeholder="≥" step="any" oninput="pmmApplyLoadsFilter()" /></th>
              <th><input class="pmm-filter-input" id="flt-my"    type="number" placeholder="≥" step="any" oninput="pmmApplyLoadsFilter()" /></th>
              <th></th><th></th><th></th>
              <th><input class="pmm-filter-input" id="flt-dcr"   type="number" placeholder="≥%" step="1"  oninput="pmmApplyLoadsFilter()" /></th>
              <th><select class="pmm-filter-select" id="flt-status" onchange="pmmApplyLoadsFilter()">
                    <option value="">All</option>
                    <option value="PASS">PASS</option>
                    <option value="FAIL">FAIL</option>
                  </select></th>
              <th></th>
            </tr>
          </thead>
          <tbody id="pmm-loads-tbody"></tbody>
        </table>
      </div>
    </div>
    <div id="pmm-loads-etabs-panel" class="hidden">
      <div class="pmm-etabs-steps">
        <span class="pmm-etabs-step"><b>1.</b> Select columns in ETABS</span>
        <span class="pmm-etabs-sep">→</span>
        <span class="pmm-etabs-step"><b>2.</b> Choose combinations below</span>
        <span class="pmm-etabs-sep">→</span>
        <span class="pmm-etabs-step"><b>3.</b> Click Import</span>
      </div>
      <div class="pmm-etabs-type-row">
        <label class="pmm-etabs-radio"><input type="radio" name="pmm-etabs-type" value="combo" checked> Combinations</label>
        <label class="pmm-etabs-radio"><input type="radio" name="pmm-etabs-type" value="case"> Load Cases</label>
        <button class="pmm-loads-btn-add" onclick="pmmEtabsFetchCombos()" style="margin-left:auto">↻ Refresh from ETABS</button>
      </div>
      <div class="pmm-etabs-search-row">
        <input type="text" id="pmm-etabs-search" class="pmm-etabs-search" placeholder="Filter combinations…" oninput="pmmEtabsFilter()" />
        <span id="pmm-etabs-count" class="pmm-etabs-count"></span>
      </div>
      <div class="pmm-etabs-combos-wrap" id="pmm-etabs-combos-wrap">
        <div class="pmm-etabs-combo-hint">Click ↻ Refresh to load combinations from ETABS</div>
      </div>
      <div class="pmm-etabs-actions">
        <button class="pmm-etabs-selall" onclick="pmmEtabsSelectAll(true)">Select All</button>
        <button class="pmm-etabs-selall" onclick="pmmEtabsSelectAll(false)">Clear All</button>
        <button class="pmm-etabs-selall" onclick="pmmEtabsSelectFiltered(true)">Select Filtered</button>
        <button class="pmm-loads-btn-check" id="btn-etabs-import" onclick="pmmEtabsImport()" style="margin-left:auto">⬇ Import Selected Members</button>
      </div>
      <div class="pmm-etabs-note" id="pmm-etabs-note"></div>
    </div>
    <div class="pmm-loads-note" id="pmm-loads-note">Enter loads and click ⚡ Check DCR after generating the diagram.</div>
  `;
  // Pre-populate with 8 blank rows
  for (let i = 0; i < 8; i++) {
    _pmmLoadId++;
    _pmmLoads.push({ id: _pmmLoadId, label: `LC${_pmmLoadId}`, P: '', Mx: '', My: '' });
  }
  pmmRenderLoadsRows();
}

function pmmAddLoad() {
  pmmSyncLoadValues();
  _pmmLoadId++;
  _pmmLoads.push({ id: _pmmLoadId, label: `LC${_pmmLoadId}`, P: '', Mx: '', My: '' });
  pmmRenderLoadsRows();
  // Scroll to bottom and focus new row label
  const wrap = document.getElementById('pmm-loads-table-wrap');
  if (wrap) wrap.scrollTop = wrap.scrollHeight;
  setTimeout(() => {
    const rows = document.querySelectorAll('#pmm-loads-tbody tr');
    rows[rows.length - 1]?.querySelector('.ld-label')?.focus();
  }, 30);
}

function pmmDeleteLoad(id) {
  pmmSyncLoadValues();
  _pmmLoads = _pmmLoads.filter(l => l.id !== id);
  pmmRenderLoadsRows();
}

function pmmCopyLoadsToClipboard() {
  pmmSyncLoadValues();
  const header = ['Label', 'P (kN)', 'Mx (kN·m)', 'My (kN·m)', 'DCR (%)', 'Status'];
  const rows = _pmmLoads
    .filter(l => l.P !== '' || l.Mx !== '' || l.My !== '')
    .map(l => [
      l.label,
      l.P  !== '' ? l.P  : 0,
      l.Mx !== '' ? l.Mx : 0,
      l.My !== '' ? l.My : 0,
      l.DCR  != null ? Math.round(parseFloat(l.DCR) * 100) : '',
      l.status != null ? l.status : ''
    ]);
  if (rows.length === 0) { alert('No load data to copy.'); return; }
  const tsv = [header, ...rows].map(r => r.join('\t')).join('\n');
  navigator.clipboard.writeText(tsv).then(() => {
    const btn = document.querySelector('.pmm-loads-btn-copy');
    if (btn) { btn.textContent = '✓ Copied'; setTimeout(() => { btn.textContent = '📋 Copy'; }, 1500); }
  }).catch(() => {
    // fallback for older browsers
    const ta = document.createElement('textarea');
    ta.value = tsv; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select(); document.execCommand('copy');
    document.body.removeChild(ta);
    const btn = document.querySelector('.pmm-loads-btn-copy');
    if (btn) { btn.textContent = '✓ Copied'; setTimeout(() => { btn.textContent = '📋 Copy'; }, 1500); }
  });
}

async function pmmExportReport() {
  if (!_pmmResult || !_pmmPayload) {
    alert('Generate the PMM diagram first before exporting.');
    return;
  }
  const btn = document.querySelector('.pmm-loads-btn-export');
  if (btn) { btn.textContent = '⏳ Exporting…'; btn.disabled = true; }

  try {
    // Capture chart images as base64 PNG
    async function chartImage(id) {
      const el = document.getElementById(id);
      if (!el || !el._fullLayout) return '';
      try { return await Plotly.toImage(el, { format: 'png', width: 800, height: 500 }); }
      catch { return ''; }
    }

    pmmSyncLoadValues();

    const [img3d, imgPmx, imgPmy] = await Promise.all([
      chartImage('pmm-chart-3d'),
      chartImage('pmm-chart-pmx'),
      chartImage('pmm-chart-pmy'),
    ]);

    // Build result summary from cached result
    const summary = {
      Pmax:    _pmmResult.Pmax    ?? '–',
      Pmin:    _pmmResult.Pmin    ?? '–',
      Ast:     _pmmResult.Ast     ?? '–',
      rho_pct: _pmmResult.rho_pct ?? '–',
    };

    const res = await fetch('/api/pmm/export-report', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${authToken()}` },
      body: JSON.stringify({
        payload:        _pmmPayload,
        loads:          _pmmLoads,
        result_summary: summary,
        chart_3d:       img3d,
        chart_pmx:      imgPmx,
        chart_pmy:      imgPmy,
      }),
    });

    if (!res.ok) {
      let msg = `Export failed (HTTP ${res.status})`;
      try { const e = await res.json(); msg = e.detail || msg; } catch {}
      throw new Error(msg);
    }

    // Trigger download
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    const cd = res.headers.get('Content-Disposition') || '';
    const fnMatch = cd.match(/filename="([^"]+)"/);
    a.download = fnMatch ? fnMatch[1] : 'PMM_Report.docx';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  } catch (err) {
    alert(`Export failed: ${err.message}`);
  } finally {
    if (btn) { btn.textContent = '📄 Export'; btn.disabled = false; }
  }
}

// ── Tab switcher ──────────────────────────────────────────────
function pmmSwitchTab(tab) {
  const manual = document.getElementById('pmm-loads-manual-panel');
  const etabs  = document.getElementById('pmm-loads-etabs-panel');
  const tabMan = document.getElementById('tab-manual');
  const tabEtabs = document.getElementById('tab-etabs');
  const btnAdd  = document.getElementById('btn-add-row');
  const btnCopy = document.querySelector('.pmm-loads-btn-copy');
  const btnCheck = document.getElementById('btn-pmm-check');
  if (tab === 'manual') {
    manual?.classList.remove('hidden');
    etabs?.classList.add('hidden');
    tabMan?.classList.add('active');
    tabEtabs?.classList.remove('active');
    if (btnAdd)  btnAdd.style.display  = '';
    if (btnCopy) btnCopy.style.display = '';
    if (btnCheck) btnCheck.style.display = '';
  } else {
    manual?.classList.add('hidden');
    etabs?.classList.remove('hidden');
    tabMan?.classList.remove('active');
    tabEtabs?.classList.add('active');
    if (btnAdd)  btnAdd.style.display  = 'none';
    if (btnCopy) btnCopy.style.display = 'none';
    if (btnCheck) btnCheck.style.display = 'none';
  }
}

// ── ETABS Import ───────────────────────────────────────────────
// ── ETABS combo prefix detection & grouping ──────────────────────────────────
function _etabsPrefix(name) {
  const m = name.match(/^([A-Za-z]+)/);
  return m ? m[1] : 'Other';
}

function _etabsGroupItems(items) {
  const map = {}, order = [];
  items.forEach(name => {
    const p = _etabsPrefix(name);
    if (!map[p]) { map[p] = []; order.push(p); }
    map[p].push(name);
  });
  return order.map(p => ({ prefix: p, items: map[p] }));
}

function pmmEtabsRenderGroups(items) {
  const wrap = document.getElementById('pmm-etabs-combos-wrap');
  if (!wrap) return;
  const groups = _etabsGroupItems(items);
  wrap.innerHTML = groups.map(g => `
    <div class="pmm-group" data-group="${g.prefix}">
      <div class="pmm-group-header" onclick="pmmEtabsToggleGroup('${g.prefix}')">
        <input type="checkbox" class="pmm-group-cb" data-group="${g.prefix}" checked
               onclick="event.stopPropagation()"
               onchange="pmmEtabsGroupCheck('${g.prefix}', this.checked)">
        <span class="pmm-group-name">${g.prefix}</span>
        <span class="pmm-group-count">(${g.items.length})</span>
        <span class="pmm-group-arrow">▼</span>
      </div>
      <div class="pmm-group-items">
        ${g.items.map(name => `
          <label class="pmm-etabs-combo-item">
            <input type="checkbox" class="pmm-etabs-cb" value="${name}" checked
                   data-group="${g.prefix}"
                   onchange="pmmEtabsUpdateGroupState('${g.prefix}')">
            <span>${name}</span>
          </label>`).join('')}
      </div>
    </div>`).join('');
  pmmEtabsUpdateCount();
}

function pmmEtabsToggleGroup(prefix) {
  const grp = document.querySelector(`.pmm-group[data-group="${prefix}"]`);
  if (!grp) return;
  const collapsed = grp.classList.toggle('collapsed');
  const arrow = grp.querySelector('.pmm-group-arrow');
  if (arrow) arrow.textContent = collapsed ? '▶' : '▼';
}

function pmmEtabsGroupCheck(prefix, checked) {
  document.querySelectorAll(`.pmm-etabs-cb[data-group="${prefix}"]`)
    .forEach(cb => cb.checked = checked);
  pmmEtabsUpdateCount();
}

function pmmEtabsUpdateGroupState(prefix) {
  const cbs = [...document.querySelectorAll(`.pmm-etabs-cb[data-group="${prefix}"]`)];
  const gcb = document.querySelector(`.pmm-group-cb[data-group="${prefix}"]`);
  if (!gcb) return;
  const n = cbs.filter(cb => cb.checked).length;
  gcb.indeterminate = n > 0 && n < cbs.length;
  gcb.checked = n === cbs.length;
  pmmEtabsUpdateCount();
}

async function pmmEtabsFetchCombos() {
  const wrap = document.getElementById('pmm-etabs-combos-wrap');
  if (!wrap) return;
  wrap.innerHTML = '<div class="pmm-etabs-combo-hint">Loading from ETABS…</div>';
  try {
    const res = await fetch('/api/pmm/etabs-combos', {
      headers: { Authorization: `Bearer ${authToken()}` }
    });
    if (!res.ok) { const e = await res.json(); throw new Error(e.detail || 'Failed'); }
    const data = await res.json();
    const type = document.querySelector('input[name="pmm-etabs-type"]:checked')?.value || 'combo';
    const items = type === 'case' ? data.cases : data.combinations;
    if (!items || items.length === 0) {
      wrap.innerHTML = '<div class="pmm-etabs-combo-hint">No items found in ETABS model.</div>';
      return;
    }
    const srch = document.getElementById('pmm-etabs-search');
    if (srch) srch.value = '';
    pmmEtabsRenderGroups(items);
  } catch (err) {
    wrap.innerHTML = `<div class="pmm-etabs-combo-hint" style="color:#f66">${err.message}</div>`;
  }
}

function pmmEtabsSelectAll(check) {
  document.querySelectorAll('.pmm-etabs-cb').forEach(cb => cb.checked = check);
  document.querySelectorAll('.pmm-group-cb').forEach(cb => {
    cb.checked = check; cb.indeterminate = false;
  });
  pmmEtabsUpdateCount();
}

function pmmEtabsSelectFiltered(check) {
  // Check/uncheck only visible items
  document.querySelectorAll('.pmm-group:not(.hidden)').forEach(grp => {
    grp.querySelectorAll('.pmm-etabs-combo-item:not(.hidden) .pmm-etabs-cb')
       .forEach(cb => cb.checked = check);
    pmmEtabsUpdateGroupState(grp.dataset.group);
  });
  pmmEtabsUpdateCount();
}

function pmmEtabsFilter() {
  const q = (document.getElementById('pmm-etabs-search')?.value || '').toLowerCase();
  document.querySelectorAll('.pmm-group').forEach(grp => {
    let anyVisible = false;
    grp.querySelectorAll('.pmm-etabs-combo-item').forEach(item => {
      const name = item.querySelector('span')?.textContent?.toLowerCase() || '';
      const show = !q || name.includes(q);
      item.classList.toggle('hidden', !show);
      if (show) anyVisible = true;
    });
    grp.classList.toggle('hidden', !anyVisible);
    // Auto-expand groups that have matches when filtering
    if (q && anyVisible) {
      grp.classList.remove('collapsed');
      const arrow = grp.querySelector('.pmm-group-arrow');
      if (arrow) arrow.textContent = '▼';
    }
  });
  pmmEtabsUpdateCount();
}

function pmmEtabsUpdateCount() {
  const total   = document.querySelectorAll('.pmm-etabs-cb').length;
  const checked = document.querySelectorAll('.pmm-etabs-cb:checked').length;
  const countEl = document.getElementById('pmm-etabs-count');
  if (countEl && total > 0) countEl.textContent = `${checked} / ${total} selected`;
}

async function pmmEtabsImport() {
  const note = document.getElementById('pmm-etabs-note');
  const btn  = document.getElementById('btn-etabs-import');
  const checked = [...document.querySelectorAll('.pmm-etabs-cb:checked')].map(cb => cb.value);
  if (checked.length === 0) {
    if (note) { note.textContent = '⚠ Select at least one combination.'; note.style.color = '#f90'; }
    return;
  }
  const loadType = document.querySelector('input[name="pmm-etabs-type"]:checked')?.value || 'combo';
  if (btn) { btn.textContent = '⏳ Importing…'; btn.disabled = true; }
  if (note) { note.textContent = ''; }
  try {
    const res = await fetch('/api/pmm/etabs-import-forces', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${authToken()}` },
      body: JSON.stringify({ combo_names: checked, load_type: loadType })
    });
    if (!res.ok) {
      let errMsg = `Import failed (HTTP ${res.status})`;
      try { const e = await res.json(); errMsg = e.detail || errMsg; } catch {}
      throw new Error(errMsg);
    }
    const data = await res.json();
    const rows = data.results || [];
    if (rows.length === 0) { throw new Error('No force data returned. Ensure columns are selected in ETABS.'); }

    // Clear existing loads before importing fresh from ETABS
    _pmmLoads = [];
    _pmmLoadId = 0;
    rows.forEach(r => {
      _pmmLoadId++;
      _pmmLoads.push({
        id:    _pmmLoadId,
        label: r.label,
        P:     r.P_kN,
        Mx:    r.M3_kNm,   // M33 → table Mx (matches engine convention)
        My:    r.M2_kNm,   // M22 → table My
      });
    });
    // Ensure at least a few blank rows remain for manual additions
    if (_pmmLoads.length < 3) {
      for (let i = _pmmLoads.length; i < 3; i++) {
        _pmmLoadId++;
        _pmmLoads.push({ id: _pmmLoadId, label: `LC${_pmmLoadId}`, P: '', Mx: '', My: '' });
      }
    }

    // Switch back to Manual tab to show imported rows
    pmmSwitchTab('manual');
    pmmRenderLoadsRows();
    pmmPopulateMxMyPDropdown();
    document.getElementById('pmm-loads-note').textContent =
      `✓ ${rows.length} load(s) imported from ETABS. Click ⚡ Check DCR to evaluate.`;
    if (note) note.textContent = '';
  } catch (err) {
    if (note) { note.textContent = `⚠ ${err.message}`; note.style.color = '#f66'; }
  } finally {
    if (btn) { btn.textContent = '⬇ Import Selected Members'; btn.disabled = false; }
  }
}

function pmmSyncLoadValues() {
  _pmmLoads.forEach(l => {
    const row = document.querySelector(`tr[data-load-id="${l.id}"]`);
    if (!row) return;
    l.label = row.querySelector('.ld-label')?.value ?? l.label;
    const pv  = row.querySelector('.ld-P')?.value;
    const mxv = row.querySelector('.ld-Mx')?.value;
    const myv = row.querySelector('.ld-My')?.value;
    l.P  = pv  !== '' ? (parseFloat(pv)  || 0) : '';
    l.Mx = mxv !== '' ? (parseFloat(mxv) || 0) : '';
    l.My = myv !== '' ? (parseFloat(myv) || 0) : '';
  });
}

// Paste from Excel: Tab-separated columns, newline-separated rows
function pmmHandlePaste(e, loadId, fieldName) {
  const raw = (e.clipboardData || window.clipboardData).getData('text/plain');
  if (!raw) return;
  // Let normal single-value paste through
  if (!raw.includes('\t') && !raw.includes('\n') && !raw.includes('\r')) return;
  e.preventDefault();
  pmmSyncLoadValues();

  const rows = raw.trim().split(/\r?\n/).map(r => r.split('\t'));
  const allFields = ['label', 'P', 'Mx', 'My'];
  const startFi   = Math.max(0, allFields.indexOf(fieldName));
  const startLi   = Math.max(0, _pmmLoads.findIndex(l => l.id === loadId));

  rows.forEach((cols, ri) => {
    const li = startLi + ri;
    // Grow list if needed
    while (_pmmLoads.length <= li) {
      _pmmLoadId++;
      _pmmLoads.push({ id: _pmmLoadId, label: `LC${_pmmLoadId}`, P: '', Mx: '', My: '' });
    }
    const load = _pmmLoads[li];
    // Clear stale DCR when data changes
    delete load.DCR; delete load.status;
    cols.forEach((val, ci) => {
      const fi = startFi + ci;
      if (fi >= allFields.length) return;
      const f = allFields[fi];
      const v = val.trim().replace(',', '.');  // handle European decimals
      if (f === 'label') { load.label = v || load.label; }
      else {
        const n = parseFloat(v);
        load[f] = isNaN(n) ? '' : n;
      }
    });
  });
  pmmRenderLoadsRows();
}

function pmmRenderLoadsRows() {
  const tbody = document.getElementById('pmm-loads-tbody');
  if (!tbody) return;
  _pmmUpdateSortHeaders();
  tbody.innerHTML = _pmmSortedLoads().map((l, idx) => {
    let dcrCell = '<span style="color:var(--t3)">–</span>';
    if (l.DCR != null) {
      const dv = parseFloat(l.DCR);
      let bg = '#00D9D9', fg = '#000';
      if      (dv > 1.0) { bg = '#CC0000'; fg = '#fff'; }
      else if (dv > 0.8) { bg = '#FF0080'; fg = '#fff'; }
      else if (dv > 0.6) { bg = '#FF8800'; fg = '#fff'; }
      else if (dv > 0.4) { bg = '#FFFF00'; fg = '#333'; }
      else if (dv > 0.2) { bg = '#00CC00'; fg = '#000'; }
      dcrCell = `<span class="pmm-dcr-chip" style="background:${bg};color:${fg}">${Math.round(dv*100)}%</span>`;
    }
    const stCell = l.status
      ? `<span class="${l.status==='PASS'?'pmm-st-pass':'pmm-st-fail'}">${l.status}</span>`
      : '<span style="color:var(--t3)">–</span>';
    const pVal  = l.P  !== '' ? l.P  : '';
    const mxVal = l.Mx !== '' ? l.Mx : '';
    const myVal = l.My !== '' ? l.My : '';
    return `<tr data-load-id="${l.id}">
      <td><span class="ld-rownum">${idx + 1}</span></td>
      <td><input class="ld-label" type="text"   value="${l.label}"
            onpaste="pmmHandlePaste(event,${l.id},'label')"
            onchange="pmmClearRowDCR(${l.id})" /></td>
      <td><input class="ld-P"     type="number" value="${pVal}"  step="any" placeholder="0"
            onpaste="pmmHandlePaste(event,${l.id},'P')"
            onchange="pmmClearRowDCR(${l.id})" /></td>
      <td><input class="ld-Mx"    type="number" value="${mxVal}" step="any" placeholder="0"
            onpaste="pmmHandlePaste(event,${l.id},'Mx')"
            onchange="pmmClearRowDCR(${l.id})" /></td>
      <td><input class="ld-My"    type="number" value="${myVal}" step="any" placeholder="0"
            onpaste="pmmHandlePaste(event,${l.id},'My')"
            onchange="pmmClearRowDCR(${l.id})" /></td>
      <td class="td-num">${l.M_demand  != null ? (+l.M_demand ).toFixed(1) : '–'}</td>
      <td class="td-num">${l.M_cap    != null ? (+l.M_cap   ).toFixed(1) : '–'}</td>
      <td class="td-num">${l.alpha_deg != null ? (+l.alpha_deg).toFixed(1) : '–'}</td>
      <td class="td-dcr">${dcrCell}</td>
      <td class="td-st">${stCell}</td>
      <td><button class="pmm-del" title="Delete row" onclick="pmmDeleteLoad(${l.id})">×</button></td>
    </tr>`;
  }).join('');
}

function pmmSortLoads(col) {
  if (_pmmSortCol === col) {
    _pmmSortDir *= -1;           // toggle direction
  } else {
    _pmmSortCol = col;
    _pmmSortDir = 1;             // new column → start ascending
  }
  pmmSyncLoadValues();
  pmmRenderLoadsRows();
  pmmApplyLoadsFilter();
}

function _pmmSortedLoads() {
  if (!_pmmSortCol) return _pmmLoads;
  const col = _pmmSortCol, dir = _pmmSortDir;
  return [..._pmmLoads].sort((a, b) => {
    let av = a[col], bv = b[col];
    if (col === 'label') return dir * String(av ?? '').localeCompare(String(bv ?? ''));
    av = av === '' || av == null ? null : parseFloat(av);
    bv = bv === '' || bv == null ? null : parseFloat(bv);
    if (av == null && bv == null) return 0;
    if (av == null) return 1;   // blanks to bottom
    if (bv == null) return -1;
    return dir * (av - bv);
  });
}

function _pmmUpdateSortHeaders() {
  ['label','P','Mx','My','DCR'].forEach(col => {
    const th = document.getElementById(`sth-${col}`);
    if (!th) return;
    // strip old indicator
    th.textContent = th.textContent.replace(/\s*[▲▼↑↓]$/, '');
    if (_pmmSortCol === col) th.textContent += _pmmSortDir === 1 ? ' ▲' : ' ▼';
  });
}

function pmmApplyLoadsFilter() {
  const label  = (document.getElementById('flt-label')?.value  || '').toLowerCase();
  const minP   = document.getElementById('flt-p')?.value;
  const minMx  = document.getElementById('flt-mx')?.value;
  const minMy  = document.getElementById('flt-my')?.value;
  const minDcr = document.getElementById('flt-dcr')?.value;
  const status = document.getElementById('flt-status')?.value || '';

  document.querySelectorAll('#pmm-loads-tbody tr').forEach(row => {
    const lbl  = (row.querySelector('.ld-label')?.value || '').toLowerCase();
    const p    = parseFloat(row.querySelector('.ld-P')?.value  || '');
    const mx   = parseFloat(row.querySelector('.ld-Mx')?.value || '');
    const my   = parseFloat(row.querySelector('.ld-My')?.value || '');
    const chip = row.querySelector('.pmm-dcr-chip');
    const dcr  = chip ? parseFloat(chip.textContent) : NaN;
    const st   = row.querySelector('.pmm-st-pass, .pmm-st-fail')?.textContent || '';

    let show = true;
    if (label  && !lbl.includes(label))               show = false;
    if (minP   !== '' && !isNaN(p)   && p   < parseFloat(minP))  show = false;
    if (minMx  !== '' && !isNaN(mx)  && mx  < parseFloat(minMx)) show = false;
    if (minMy  !== '' && !isNaN(my)  && my  < parseFloat(minMy)) show = false;
    if (minDcr !== '' && !isNaN(dcr) && dcr < parseFloat(minDcr)) show = false;
    if (status && st !== status)                       show = false;

    row.style.display = show ? '' : 'none';
  });
}

function pmmClearRowDCR(id) {
  const l = _pmmLoads.find(x => x.id === id);
  if (l) { delete l.DCR; delete l.status; delete l.M_cap; delete l.M_demand; delete l.alpha_deg; }
}

async function pmmCheckLoads() {
  if (!_pmmResult) {
    const note = document.getElementById('pmm-loads-note');
    if (note) note.textContent = '⚠ Generate the PMM diagram first, then click Check DCR.';
    return;
  }
  pmmSyncLoadValues();
  // Only send rows that have at least a P value entered
  const activeDemands = _pmmLoads.filter(l => l.P !== '');
  if (!activeDemands.length) {
    const note = document.getElementById('pmm-loads-note');
    if (note) note.textContent = '⚠ Enter at least one load (P value required).';
    return;
  }
  const btn = document.getElementById('btn-pmm-check');
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  try {
    const res = await authFetch('/api/pmm/check', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      // Engine convention: Mx=weak-axis(M22), My=strong-axis(M33)
      // Table stores user convention: l.Mx=M33, l.My=M22 → swap here
      body:    JSON.stringify({ demands: activeDemands.map(l => ({
        label: l.label, P: -(+l.P), Mx: +(l.My || 0), My: +(l.Mx || 0)
      })) }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Check failed');
    // Map results back — preserve user-entered P (ETABS sign: compression = negative)
    data.results.forEach((r, i) => {
      const load = activeDemands[i];
      if (load) { load.M_demand = r.M_demand; load.alpha_deg = r.alpha_deg;
                  load.M_cap = r.M_cap; load.DCR = r.DCR; load.status = r.status; }
    });
    // Replace engine DCR with boundary-based DCR (each demand at its own P level)
    pmmUpdateDCRFromBoundary(activeDemands);
    pmmPopulateMxMyPDropdown();
    pmmRenderLoadsRows();
    const checkedLoads = activeDemands.filter(l => l.status);
    pmmRender3D(_pmmResult, _pmmPayload, checkedLoads);
    pmmRender2D(_pmmResult, 'pmx', 'P–Mx', 'Mx', _pmmPayload, checkedLoads);
    pmmRender2D(_pmmResult, 'pmy', 'P–My', 'My', _pmmPayload, checkedLoads);
    const note = document.getElementById('pmm-loads-note');
    const nFail = checkedLoads.filter(l => l.status === 'FAIL').length;
    if (note) note.textContent = nFail > 0
      ? `⚠ ${nFail} load(s) FAIL. Review DCR column.`
      : `✓ All ${checkedLoads.length} load(s) PASS.`;
  } catch(e) {
    alert('Check error: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '⚡ Check DCR'; }
  }
}

// ── PMM Design Optimizer ─────────────────────────────────────────────────────

async function pmmOptimize() {
  if (!_pmmResult) { alert('Generate diagram first.'); return; }
  pmmSyncLoadValues();
  const activeDemands = _pmmLoads.filter(l => l.P !== '');
  if (!activeDemands.length) { alert('Enter at least one demand load (P value required).'); return; }

  const btn      = document.getElementById('btn-pmm-optimize');
  const resultEl = document.getElementById('pmm-opt-result');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Optimizing…'; }
  if (resultEl) resultEl.classList.add('hidden');

  try {
    const targetDCR = (parseFloat(document.getElementById('pmm-opt-dcr')?.value)    || 90)  / 100;
    const minRho    = (parseFloat(document.getElementById('pmm-opt-minrho')?.value) || 1.0);
    const maxRho    = (parseFloat(document.getElementById('pmm-opt-maxrho')?.value) || 4.0);

    // Read resolution from the UI dropdown so optimizer uses the same surface as Check DCR
    const resSel = document.getElementById('pmm-resolution');
    const [optAlphaSteps, optNumPoints] = (resSel?.value || '10:70').split(':').map(Number);

    const body = {
      b_mm:        parseFloat(document.getElementById('pmm-b').value),
      h_mm:        parseFloat(document.getElementById('pmm-h').value),
      fc_mpa:      parseFloat(document.getElementById('pmm-fc').value),
      fy_mpa:      parseFloat(document.getElementById('pmm-fy').value),
      Es_mpa:      parseFloat(document.getElementById('pmm-es').value) || 200000,
      cover_mm:       parseFloat(document.getElementById('pmm-cover').value),
      stirrup_dia_mm: parseFloat(document.getElementById('pmm-stirrup-dia')?.value) || 10,
      include_phi: document.getElementById('pmm-phi')?.checked ?? true,
      bar_size:    document.getElementById('pmm-barsize')?.value || 'Ø20',
      target_dcr:  targetDCR,
      min_rho_pct: minRho,
      max_rho_pct: maxRho,
      alpha_steps: optAlphaSteps,  // match UI resolution so optimizer DCR = Check DCR
      num_points:  optNumPoints,
      // Engine: Mx=weak(M22), My=strong(M33); table: Mx=M33, My=M22 → swap
      demands: activeDemands.map(l => ({ label: l.label, P: -(+l.P), Mx: +(l.My||0), My: +(l.Mx||0) })),
    };

    const res  = await authFetch('/api/pmm/optimize', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Optimization failed');

    // ── Min clear spacing (ACI §25.8.1) ──────────────────────────────────
    const minOk  = data.min_clear_mm >= data.min_clear_req;
    const minCol = minOk ? '#4ade80' : '#f97316';

    // ── Max clear spacing (ACI §25.7.2.3) — all faces ────────────────────
    const maxOk  = data.max_clear_mm <= (data.max_clear_req ?? 150);
    const maxCol = maxOk ? '#4ade80' : '#f97316';
    const maxSpacingRow = `<div class="pmm-opt-row">
         <span class="pmm-opt-lbl">Max spacing</span>
         <strong style="color:${maxCol}">${maxOk?'✓':'⚠'} ${data.max_clear_mm} mm
           <span class="pmm-opt-note">(ACI max ${data.max_clear_req ?? 150} mm)</span>
         </strong>
       </div>`;

    // ── Target not achievable warning ─────────────────────────────────────
    const targetNote = !data.target_met
      ? `<div class="pmm-opt-grew">⚠ Target DCR not achievable with ${data.bar_size} at ρ ${minRho}–${maxRho}%. Showing max-capacity arrangement.</div>`
      : '';

    resultEl.innerHTML = `
      <div class="pmm-opt-title">✦ Optimal Design</div>
      <div class="pmm-opt-row">
        <span class="pmm-opt-lbl">Arrangement</span>
        <strong>${data.arrangement}</strong>
      </div>
      <div class="pmm-opt-row">
        <span class="pmm-opt-lbl">Bar size</span>
        <strong>${data.bar_size}</strong>
      </div>
      <div class="pmm-opt-row">
        <span class="pmm-opt-lbl">ρ</span>
        <strong>${data.rho_pct}%</strong>
      </div>
      <div class="pmm-opt-row">
        <span class="pmm-opt-lbl">Min spacing</span>
        <strong style="color:${minCol}">${minOk?'✓':'⚠'} ${data.min_clear_mm} mm
          <span class="pmm-opt-note">(ACI min ${data.min_clear_req} mm)</span>
        </strong>
      </div>
      ${maxSpacingRow}
      <div class="pmm-opt-row">
        <span class="pmm-opt-lbl">Achieved DCR</span>
        <strong class="pmm-opt-dcr-val">${Math.round(data.achieved_dcr)}%</strong>
      </div>
      ${targetNote}
      <button class="pmm-opt-apply-btn"
        onclick="pmmApplyOptimized(${data.b_mm},${data.h_mm},'${data.bar_size}',${data.nbars_b},${data.nbars_h})">
        ✓ Apply to Section
      </button>`;
    resultEl.classList.remove('hidden');

  } catch(e) {
    alert('Optimize error: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '⚡ Optimize'; }
  }
}

async function pmmApplyOptimized(b, h, barSize, nbarsB, nbarsH) {
  // 1. Update section inputs
  document.getElementById('pmm-b').value = b;
  document.getElementById('pmm-h').value = h;
  if (nbarsB != null) document.getElementById('pmm-nbars-b').value = nbarsB;
  if (nbarsH != null) document.getElementById('pmm-nbars-h').value = nbarsH;
  const sel = document.getElementById('pmm-barsize');
  if (sel) {
    for (const opt of sel.options) {
      if (opt.value === barSize) { sel.value = barSize; break; }
    }
  }

  // 2. Refresh ρ info and section preview immediately
  pmmUpdateRhoInfo();
  pmmDrawSection();

  // 3. Hide the result card
  document.getElementById('pmm-opt-result')?.classList.add('hidden');

  // 4. Generate diagram with the updated section
  await pmmGenerate();

  // 5. Auto-check loads so DCR badges refresh straight away
  if (_pmmLoads.length > 0) {
    await pmmCheckLoads();
  }
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('btn-cleaner-browse')?.addEventListener('click', cleanerBrowse);
  document.getElementById('btn-cleaner-scan')?.addEventListener('click', cleanerScan);
  document.getElementById('btn-cleaner-delete')?.addEventListener('click', cleanerDelete);
  document.getElementById('btn-cleaner-toggle-list')?.addEventListener('click', () => {
    const list = document.getElementById('cleaner-file-list');
    const btn  = document.getElementById('btn-cleaner-toggle-list');
    const hidden = list.classList.toggle('hidden');
    btn.textContent = hidden ? 'Show files' : 'Hide files';
  });
  pmmInit();
});


// ================================================================
//  PMM BATCH CHECK MODULE
// ================================================================

let _pmmBatchItems = [];      // [{label, value}] — current combo/case list
let _pmmBatchChecked = new Set(); // values currently checked (persists across filter changes)
let _pmmBatchResults = null;  // last batch result
let _pmmBatchUsedCombos = []; // combos that were used in the last batch run

// ── Open / Close ──────────────────────────────────────────────────
function pmmBatchOpen() {
  document.getElementById('pmm-batch-modal').classList.remove('hidden');
  pmmBatchShowStep(1);
}

function pmmBatchClose() {
  document.getElementById('pmm-batch-modal').classList.add('hidden');
}

function pmmBatchShowStep(n) {
  document.getElementById('pmm-batch-step1').classList.toggle('hidden', n !== 1);
  document.getElementById('pmm-batch-step2').classList.toggle('hidden', n !== 2);
  document.getElementById('pmm-batch-step3').classList.toggle('hidden', n !== 3);
}

function pmmBatchReset() { pmmBatchShowStep(1); }

// ── Fetch combos from ETABS ───────────────────────────────────────
async function pmmBatchFetchCombos() {
  const wrap = document.getElementById('pmm-batch-combos-wrap');
  const note = document.getElementById('pmm-batch-note');
  wrap.innerHTML = '<div class="pmm-etabs-combo-hint">Loading from ETABS…</div>';
  if (note) note.textContent = '';
  try {
    const res = await authFetch('/api/pmm/etabs-combos');
    if (!res.ok) { const e = await res.json(); throw new Error(e.detail || 'Failed'); }
    const data = await res.json();
    const type = document.querySelector('input[name="pmm-batch-type"]:checked')?.value || 'combo';
    const items = (type === 'case' ? data.cases : data.combinations) || [];
    if (!items.length) {
      wrap.innerHTML = '<div class="pmm-etabs-combo-hint">No items found in ETABS model.</div>';
      return;
    }
    _pmmBatchItems = items.map(v => ({ label: v, value: v }));
    _pmmBatchChecked = new Set(items); // all checked by default
    const srch = document.getElementById('pmm-batch-search');
    if (srch) srch.value = '';
    pmmBatchRenderList(_pmmBatchItems);
  } catch (err) {
    wrap.innerHTML = `<div class="pmm-etabs-combo-hint" style="color:#f66">${err.message}</div>`;
  }
}

function pmmBatchRenderList(items) {
  const wrap = document.getElementById('pmm-batch-combos-wrap');
  const count = document.getElementById('pmm-batch-count');
  if (!items.length) {
    wrap.innerHTML = '<div class="pmm-etabs-combo-hint">No matches.</div>';
    if (count) count.textContent = '';
    return;
  }
  wrap.innerHTML = items.map(it => `
    <label class="pmm-etabs-combo-item">
      <input type="checkbox" class="pmm-batch-cb" value="${it.value}" ${_pmmBatchChecked.has(it.value) ? 'checked' : ''}>
      <span>${it.label}</span>
    </label>`).join('');
  pmmBatchUpdateCount();
}

function pmmBatchFilter() {
  const q = (document.getElementById('pmm-batch-search')?.value || '').toLowerCase();
  document.querySelectorAll('#pmm-batch-combos-wrap .pmm-etabs-combo-item').forEach(item => {
    const name = item.querySelector('span')?.textContent?.toLowerCase() || '';
    item.classList.toggle('hidden', !!q && !name.includes(q));
  });
  pmmBatchUpdateCount();
}

function pmmBatchUpdateCount() {
  const total   = document.querySelectorAll('.pmm-batch-cb').length;
  const checked = document.querySelectorAll('.pmm-batch-cb:checked').length;
  const countEl = document.getElementById('pmm-batch-count');
  if (countEl && total > 0) countEl.textContent = `${checked} / ${total} selected`;
}

function pmmBatchSelectAll(state) {
  if (state) _pmmBatchItems.forEach(it => _pmmBatchChecked.add(it.value));
  else _pmmBatchChecked.clear();
  document.querySelectorAll('.pmm-batch-cb').forEach(cb => { cb.checked = state; });
  pmmBatchUpdateCount();
}

function pmmBatchSelectFiltered(state) {
  // Only affect currently visible (non-hidden) items
  document.querySelectorAll('#pmm-batch-combos-wrap .pmm-etabs-combo-item:not(.hidden) .pmm-batch-cb').forEach(cb => {
    cb.checked = state;
    if (state) _pmmBatchChecked.add(cb.value);
    else _pmmBatchChecked.delete(cb.value);
  });
  pmmBatchUpdateCount();
}

// Track individual checkbox toggles (event delegation on wrap)
document.getElementById('pmm-batch-combos-wrap')?.addEventListener('change', e => {
  if (e.target.classList.contains('pmm-batch-cb')) {
    if (e.target.checked) _pmmBatchChecked.add(e.target.value);
    else _pmmBatchChecked.delete(e.target.value);
    pmmBatchUpdateCount();
  }
});

// ── Check individual section from batch results ───────────────────
async function pmmBatchCheckIndividual(sectionName) {
  pmmBatchClose();
  // Navigate to PMM panel
  document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  const navBtn = document.querySelector('.nav-item[data-target="pmm-panel"]');
  if (navBtn) navBtn.classList.add('active');
  document.getElementById('pmm-panel')?.classList.add('active');
  try {
    const res = await authFetch('/api/pmm/etabs-sections');
    if (!res.ok) throw new Error('Failed to fetch sections');
    const data = await res.json();
    const sections = data.sections || [];
    const sec = sections.find(s => (s.name || s.prop_name || '').toLowerCase() === sectionName.toLowerCase());
    if (!sec) { pmmSetStatus(`Section "${sectionName}" not found in ETABS.`, 'error'); return; }
    await pmmFillFromSection(sec, _pmmBatchUsedCombos.length ? _pmmBatchUsedCombos : null);
    pmmSetStatus(`Loaded: ${sectionName}`, '');
    // Generate diagram then check DCR, then switch to 3D surface tab
    await pmmGenerate();
    await pmmCheckLoads();
    pmmShowTab('3d');
  } catch (e) {
    pmmSetStatus(e.message, 'error');
  }
}

// ── Run ───────────────────────────────────────────────────────────
async function pmmBatchRun() {
  const note = document.getElementById('pmm-batch-note');
  const checked = [...document.querySelectorAll('.pmm-batch-cb:checked')].map(cb => cb.value);
  if (!checked.length) {
    if (note) { note.textContent = '⚠ Select at least one combination.'; note.style.color = '#f90'; }
    return;
  }
  const loadType = document.querySelector('input[name="pmm-batch-type"]:checked')?.value || 'combo';
  _pmmBatchUsedCombos = checked; // store for "→ Check" individual use
  if (note) note.textContent = '';

  pmmBatchShowStep(2);

  try {
    const res = await authFetch('/api/pmm/etabs-batch-check', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ combo_names: checked, load_type: loadType }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);

    _pmmBatchResults = data;
    pmmBatchRenderResults(data);
    pmmRenderBatchResultsTab();
    pmmBatchShowStep(3);
  } catch (err) {
    pmmBatchShowStep(1);
    if (note) { note.textContent = `⚠ ${err.message}`; note.style.color = '#f66'; }
  }
}

// ── Render results table ──────────────────────────────────────────
function pmmBatchRenderResults(data) {
  const cols = data.columns || [];
  const nFail = cols.filter(c => c.status === 'FAIL').length;
  const nPass = cols.filter(c => c.status === 'PASS').length;

  const summEl = document.getElementById('pmm-batch-summary-text');
  if (summEl) {
    summEl.innerHTML = `${cols.length} section(s) checked — `
      + `<span style="color:#16a34a;font-weight:700">${nPass} PASS</span>`
      + (nFail ? ` · <span style="color:#ef4444;font-weight:700">${nFail} FAIL</span>` : '');
  }

  const tbody = document.getElementById('pmm-batch-tbody');
  if (!tbody) return;

  tbody.innerHTML = cols.map(c => {
    if (c.error) {
      const secEscErr = c.section.replace(/'/g, "\\'");
      return `<tr style="background:#fff1f2">
        <td class="pmm-batch-td" style="text-align:left">${c.section}</td>
        <td class="pmm-batch-td" colspan="10" style="color:#ef4444">${c.error}</td>
        <td class="pmm-batch-td">
          <button class="btn btn-ghost btn-sm" style="font-size:10px;padding:2px 8px;white-space:nowrap"
                  onclick="pmmBatchCheckIndividual('${secEscErr}')">→ Check</button>
        </td>
      </tr>`;
    }
    const statusColor = c.status === 'FAIL' ? '#ef4444'
                      : c.status === 'PASS' ? '#16a34a' : '#94a3b8';
    const rowBg = c.status === 'FAIL' ? 'background:#fff1f2' : '';
    const w = c.worst || {};
    const dcrTxt = c.max_dcr != null ? c.max_dcr.toFixed(3) : '—';
    const dcrColor = c.max_dcr > 1.0 ? '#ef4444' : c.max_dcr > 0.9 ? '#f59e0b' : '#16a34a';
    const secEsc = c.section.replace(/'/g, "\\'");
    const barInfo = c.nbars != null ? `${c.nbars}${c.rebar_size ? '-' + c.rebar_size : ''}` : '—';
    return `<tr style="${rowBg}">
      <td class="pmm-batch-td" style="text-align:left;font-weight:600">${c.section}</td>
      <td class="pmm-batch-td">${Math.round(c.b_mm)}×${Math.round(c.h_mm)}</td>
      <td class="pmm-batch-td" style="font-size:11px">${barInfo}</td>
      <td class="pmm-batch-td">${(c.rho_pct || 0).toFixed(2)}</td>
      <td class="pmm-batch-td">${(c.phi_Pn_max_kN || 0).toFixed(0)}</td>
      <td class="pmm-batch-td">${c.n_frames ?? '—'}</td>
      <td class="pmm-batch-td" style="font-size:10px;max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${w.combo||''}">${w.combo || '—'}</td>
      <td class="pmm-batch-td">${w.P_kN != null ? w.P_kN.toFixed(1) : '—'}</td>
      <td class="pmm-batch-td">${w.Mx_kNm != null ? w.Mx_kNm.toFixed(1) : '—'}</td>
      <td class="pmm-batch-td">${w.My_kNm != null ? w.My_kNm.toFixed(1) : '—'}</td>
      <td class="pmm-batch-td" style="font-weight:700;color:${dcrColor}">${dcrTxt}</td>
      <td class="pmm-batch-td">
        <span style="display:inline-block;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:700;
                     background:${statusColor}1a;color:${statusColor};border:1px solid ${statusColor}44">
          ${c.status}
        </span>
      </td>
      <td class="pmm-batch-td">
        <button class="btn btn-ghost btn-sm" style="font-size:10px;padding:2px 8px;white-space:nowrap"
                onclick="pmmBatchCheckIndividual('${secEsc}')">→ Check</button>
      </td>
    </tr>`;
  }).join('');
}

// ── Batch Results Tab ─────────────────────────────────────────────
function pmmRenderBatchResultsTab() {
  if (!_pmmBatchResults) return;
  const data = _pmmBatchResults;
  const cols = data.columns || [];
  const nFail = cols.filter(c => c.status === 'FAIL').length;
  const nPass = cols.filter(c => c.status === 'PASS').length;

  // Update summary in tab toolbar
  const summEl = document.getElementById('pmm-batch-results-summary');
  if (summEl) {
    summEl.innerHTML = `${cols.length} section(s) — `
      + `<span style="color:#16a34a;font-weight:700">${nPass} PASS</span>`
      + (nFail ? ` · <span style="color:#ef4444;font-weight:700">${nFail} FAIL</span>` : '');
  }

  const tbody = document.getElementById('pmm-batch-results-tbody');
  if (!tbody) return;

  tbody.innerHTML = cols.map(c => {
    if (c.error) {
      const secEscErr = c.section.replace(/'/g, "\\'");
      return `<tr style="background:#fff1f2">
        <td class="pmm-batch-td" style="text-align:left">${c.section}</td>
        <td class="pmm-batch-td" colspan="10" style="color:#ef4444">${c.error}</td>
        <td class="pmm-batch-td">
          <button class="btn btn-ghost btn-sm" style="font-size:10px;padding:2px 8px;white-space:nowrap"
                  onclick="pmmBatchCheckIndividual('${secEscErr}')">→ Check</button>
        </td>
      </tr>`;
    }
    const statusColor = c.status === 'FAIL' ? '#ef4444'
                      : c.status === 'PASS' ? '#16a34a' : '#94a3b8';
    const rowBg = c.status === 'FAIL' ? 'background:#fff1f2' : '';
    const w = c.worst || {};
    const dcrTxt = c.max_dcr != null ? c.max_dcr.toFixed(3) : '—';
    const dcrColor = c.max_dcr > 1.0 ? '#ef4444' : c.max_dcr > 0.9 ? '#f59e0b' : '#16a34a';
    const secEsc = c.section.replace(/'/g, "\\'");
    const barInfo = c.nbars != null ? `${c.nbars}${c.rebar_size ? '-' + c.rebar_size : ''}` : '—';
    return `<tr style="${rowBg}">
      <td class="pmm-batch-td" style="text-align:left;font-weight:600">${c.section}</td>
      <td class="pmm-batch-td">${Math.round(c.b_mm)}×${Math.round(c.h_mm)}</td>
      <td class="pmm-batch-td" style="font-size:11px">${barInfo}</td>
      <td class="pmm-batch-td">${(c.rho_pct || 0).toFixed(2)}</td>
      <td class="pmm-batch-td">${(c.phi_Pn_max_kN || 0).toFixed(0)}</td>
      <td class="pmm-batch-td">${c.n_frames ?? '—'}</td>
      <td class="pmm-batch-td" style="font-size:10px;max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${w.combo||''}">${w.combo || '—'}</td>
      <td class="pmm-batch-td">${w.P_kN != null ? w.P_kN.toFixed(1) : '—'}</td>
      <td class="pmm-batch-td">${w.Mx_kNm != null ? w.Mx_kNm.toFixed(1) : '—'}</td>
      <td class="pmm-batch-td">${w.My_kNm != null ? w.My_kNm.toFixed(1) : '—'}</td>
      <td class="pmm-batch-td" style="font-weight:700;color:${dcrColor}">${dcrTxt}</td>
      <td class="pmm-batch-td">
        <span style="display:inline-block;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:700;
                     background:${statusColor}1a;color:${statusColor};border:1px solid ${statusColor}44">
          ${c.status}
        </span>
      </td>
      <td class="pmm-batch-td">
        <button class="btn btn-ghost btn-sm" style="font-size:10px;padding:2px 8px;white-space:nowrap"
                onclick="pmmBatchCheckIndividual('${secEsc}')">→ Check</button>
      </td>
    </tr>`;
  }).join('');

  // Hide placeholder, show toolbar + table
  document.getElementById('pmm-batch-no-results')?.classList.add('hidden');
  document.getElementById('pmm-batch-results-toolbar')?.classList.remove('hidden');
  document.getElementById('pmm-batch-results-table-wrap')?.classList.remove('hidden');

  // Show the sidebar "View Batch Results" button
  document.getElementById('btn-pmm-batch-results')?.classList.remove('hidden');

}

// ── Batch Results Filter & Sort ───────────────────────────────────
let _pmmBrSortCol = -1, _pmmBrSortAsc = true;

function pmmBrFilter() {
  const filters = [...document.querySelectorAll('#pmm-batch-results-thead .pmm-br-filter')]
    .map(inp => inp.value.trim().toLowerCase());
  const rows = document.querySelectorAll('#pmm-batch-results-tbody tr');
  rows.forEach(row => {
    const cells = [...row.querySelectorAll('td')];
    const match = filters.every((f, i) => {
      if (!f) return true;
      const cell = cells[i];
      return cell ? cell.textContent.toLowerCase().includes(f) : true;
    });
    row.style.display = match ? '' : 'none';
  });
}

function pmmBrSort(col) {
  const thead = document.getElementById('pmm-batch-results-thead');
  if (!thead) return;
  const sortBtns = thead.querySelectorAll('.pmm-br-sort');

  if (_pmmBrSortCol === col) {
    _pmmBrSortAsc = !_pmmBrSortAsc;
  } else {
    _pmmBrSortCol = col;
    _pmmBrSortAsc = true;
  }

  sortBtns.forEach(btn => {
    btn.classList.remove('active','asc','desc');
    if (+btn.dataset.col === col) btn.classList.add('active', _pmmBrSortAsc ? 'asc' : 'desc');
  });

  const tbody = document.getElementById('pmm-batch-results-tbody');
  if (!tbody) return;
  const rows = [...tbody.querySelectorAll('tr')];
  rows.sort((a, b) => {
    const aText = (a.querySelectorAll('td')[col]?.textContent || '').trim();
    const bText = (b.querySelectorAll('td')[col]?.textContent || '').trim();
    const aNum = parseFloat(aText.replace(/[^\d.\-]/g, ''));
    const bNum = parseFloat(bText.replace(/[^\d.\-]/g, ''));
    let cmp;
    if (!isNaN(aNum) && !isNaN(bNum)) {
      cmp = aNum - bNum;
    } else {
      cmp = aText.localeCompare(bText);
    }
    return _pmmBrSortAsc ? cmp : -cmp;
  });
  rows.forEach(r => tbody.appendChild(r));
}

// ── Export CSV ────────────────────────────────────────────────────
function pmmBatchExportCSV() {
  if (!_pmmBatchResults) return;
  const cols = _pmmBatchResults.columns || [];
  const rows = [
    ['Section','b (mm)','h (mm)','ρ (%)','φPn,max (kN)',
     'Frames','Worst Combo','P (kN)','Mx (kN·m)','My (kN·m)','Max DCR','Status'],
    ...cols.map(c => {
      const w = c.worst || {};
      return [
        c.section,
        Math.round(c.b_mm), Math.round(c.h_mm),
        (c.rho_pct||0).toFixed(2),
        (c.phi_Pn_max_kN||0).toFixed(0),
        c.n_frames ?? '',
        w.combo || '',
        w.P_kN  != null ? w.P_kN.toFixed(1)  : '',
        w.Mx_kNm != null ? w.Mx_kNm.toFixed(1) : '',
        w.My_kNm != null ? w.My_kNm.toFixed(1) : '',
        c.max_dcr != null ? c.max_dcr.toFixed(3) : '',
        c.status || '',
      ];
    }),
  ];
  const csv = rows.map(r => r.join(',')).join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const url  = URL.createObjectURL(blob);
  const a    = Object.assign(document.createElement('a'), { href: url, download: 'pmm_batch_check.csv' });
  document.body.appendChild(a); a.click();
  document.body.removeChild(a); URL.revokeObjectURL(url);
}

// Close modal on overlay click
document.getElementById('pmm-batch-modal')?.addEventListener('click', e => {
  if (e.target === document.getElementById('pmm-batch-modal')) pmmBatchClose();
});

// Re-fetch when switching combo/case radio
document.querySelectorAll('input[name="pmm-batch-type"]').forEach(r => {
  r.addEventListener('change', () => {
    if (_pmmBatchItems.length) pmmBatchFetchCombos();
  });
});

