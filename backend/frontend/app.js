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
  // Keep global plan state in sync and refresh nav locks
  currentUserPlan = plan;
  updateNavLocks(plan);
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
    if (data.plan) updatePlanBadge(data.plan);
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
  const btn = document.createElement('button');
  btn.className   = 'lc-del-btn';
  btn.innerHTML   = '&times;';
  btn.title       = 'Delete row';
  btn.addEventListener('click', () => { rowEl.remove(); lcUpdateRowCount(); lcUpdateEmptyState(); });
  td.appendChild(btn);
  return td;
}

// ── Add row ──
function lcAddRow(template = null) {
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
  tbody.appendChild(tr);

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
  ['rcc-gen-depth','rcc-gen-width'].forEach(id => {
    document.getElementById(id).addEventListener('input', rccUpdateGenName);
  });
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
    const scale  = Math.min((cellW - 60) / maxDim, (cellH - 80) / maxDim);
    const cW     = s.depth * scale;
    const cH     = s.width * scale;
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
