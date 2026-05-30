(function () {
  const STORAGE_KEYS = {
    accessToken: 'yggdrasil-access-token',
    refreshToken: 'yggdrasil-refresh-token',
    user: 'yggdrasil-auth-user',
  };

  const apiBase = location.protocol === 'file:' ? 'http://127.0.0.1:8000' : '';

  function readValue(key) {
    try {
      return window.localStorage.getItem(key) || '';
    } catch (error) {
      return '';
    }
  }

  function writeValue(key, value) {
    try {
      if (value) {
        window.localStorage.setItem(key, value);
      } else {
        window.localStorage.removeItem(key);
      }
    } catch (error) {
      // Ignore storage failures.
    }
  }

  function readJson(key) {
    const raw = readValue(key);
    if (!raw) {
      return null;
    }
    try {
      return JSON.parse(raw);
    } catch (error) {
      return null;
    }
  }

  function writeJson(key, value) {
    try {
      if (value == null) {
        window.localStorage.removeItem(key);
        return;
      }
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch (error) {
      // Ignore storage failures.
    }
  }

  const state = {
    user: readJson(STORAGE_KEYS.user),
    shell: null,
    modal: null,
    activeTab: 'login',
    signinButton: null,
    signoutButton: null,
    badge: null,
    loginForm: null,
    registerForm: null,
    loginStatus: null,
    registerStatus: null,
    title: null,
    subtitle: null,
  };

  function getAccessToken() {
    return readValue(STORAGE_KEYS.accessToken);
  }

  function getRefreshToken() {
    return readValue(STORAGE_KEYS.refreshToken);
  }

  function hasSession() {
    return Boolean(getAccessToken() || getRefreshToken());
  }

  function setSession(session, profile) {
    if (!session) {
      writeValue(STORAGE_KEYS.accessToken, '');
      writeValue(STORAGE_KEYS.refreshToken, '');
      writeJson(STORAGE_KEYS.user, null);
      state.user = null;
      renderShell();
      return;
    }

    writeValue(STORAGE_KEYS.accessToken, session.access_token || '');
    writeValue(STORAGE_KEYS.refreshToken, session.refresh_token || '');
    state.user = profile || session.user || null;
    writeJson(STORAGE_KEYS.user, state.user);
    renderShell();
  }

  function injectStyles() {
    if (document.getElementById('yggdrasil-auth-styles')) {
      return;
    }

    const style = document.createElement('style');
    style.id = 'yggdrasil-auth-styles';
    style.textContent = `
      .auth-shell{margin-left:auto;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
      .hidden{display:none!important}
      .auth-badge{font-size:11px;border:0.5px solid var(--color-border-tertiary);border-radius:999px;padding:7px 10px;background:var(--color-background-secondary);color:var(--color-text-secondary);display:inline-flex;align-items:center;gap:6px}
      .auth-badge strong{color:var(--color-text-primary);font-weight:500}
      .auth-button{font-size:12px;border:0.5px solid var(--color-border-secondary);border-radius:999px;padding:8px 12px;background:var(--color-background-secondary);color:var(--color-text-primary);cursor:pointer;font-family:var(--sans);opacity:1}
      .auth-button.secondary{background:var(--color-background-secondary);color:var(--color-text-primary);border-color:var(--color-border-secondary)}
      .auth-button.danger{background:var(--color-accent-danger-soft);color:var(--color-accent-danger);border-color:var(--color-accent-danger-border)}
      .auth-button:hover{filter:brightness(.98)}
      .auth-overlay{position:fixed;inset:0;background:var(--color-overlay);display:flex;align-items:center;justify-content:center;padding:20px;z-index:30}
      .auth-overlay.hidden{display:none!important}
      .auth-card{width:min(100%,560px);background:var(--color-background-primary);border:0.5px solid var(--color-border-tertiary);border-radius:22px;box-shadow:0 24px 80px rgba(15,23,42,.22);padding:18px 18px 16px;opacity:1}
      .auth-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:14px}
      .auth-title{font-size:16px;font-weight:500;line-height:1.35}
      .auth-sub{font-size:12px;color:var(--color-text-secondary);line-height:1.6;margin-top:3px}
      .auth-close{border:0.5px solid var(--color-border-tertiary);background:var(--color-background-secondary);border-radius:10px;width:32px;height:32px;cursor:pointer;color:var(--color-text-secondary)}
      .auth-tabs{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
      .auth-tab{border:0.5px solid var(--color-border-secondary);background:var(--color-background-secondary);color:var(--color-text-secondary);border-radius:999px;padding:7px 12px;font-size:12px;cursor:pointer}
      .auth-tab.active{background:var(--color-background-info);color:var(--color-text-info);border-color:var(--color-border-info)}
      .auth-form{display:none;flex-direction:column;gap:12px}
      .auth-form.active{display:flex}
      .auth-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
      .auth-field{display:flex;flex-direction:column;gap:6px}
      .auth-field label{font-size:11px;color:var(--color-text-secondary);text-transform:uppercase;letter-spacing:.06em}
      .auth-field input{width:100%;font-family:var(--sans);font-size:13px;padding:10px 12px;border:0.5px solid var(--color-border-secondary);border-radius:var(--border-radius-md);background:var(--color-background-secondary);color:var(--color-text-primary);outline:none}
      .auth-field input:focus{border-color:var(--color-border-primary)}
      .auth-field.full{grid-column:1 / -1}
      .auth-note{font-size:11px;color:var(--color-text-tertiary);line-height:1.5}
      .auth-status{font-size:12px;line-height:1.5;padding:10px 12px;border:0.5px solid var(--color-border-tertiary);border-radius:var(--radius-md);background:var(--color-background-secondary);color:var(--color-text-secondary)}
      .auth-status.ok{color:var(--color-text-success);border-color:rgba(29,158,117,.25);background:rgba(29,158,117,.08)}
      .auth-status.bad{color:var(--color-accent-danger);border-color:var(--color-accent-danger-border);background:var(--color-accent-danger-soft)}
      .auth-actions{display:flex;justify-content:flex-end;gap:10px;flex-wrap:wrap;margin-top:4px}
      @media (max-width: 560px){.auth-grid{grid-template-columns:1fr}}
    `;
    document.head.appendChild(style);
  }

  function buildModal() {
    if (state.modal) {
      return;
    }

    state.modal = document.createElement('div');
    state.modal.className = 'auth-overlay hidden';
    state.modal.innerHTML = `
      <div class="auth-card" role="dialog" aria-modal="true" aria-labelledby="auth-title">
        <div class="auth-head">
          <div>
            <div class="auth-title" id="auth-title">Sign in to continue</div>
            <div class="auth-sub" id="auth-sub">Use your student account to unlock chat, uploads, and profile sync.</div>
          </div>
          <button type="button" class="auth-close" id="auth-close" aria-label="Close auth dialog">×</button>
        </div>
        <div class="auth-tabs">
          <button type="button" class="auth-tab active" data-auth-tab="login">Login</button>
          <button type="button" class="auth-tab" data-auth-tab="register">Register</button>
        </div>
        <form class="auth-form active" id="auth-login-form">
          <div class="auth-grid">
            <div class="auth-field full">
              <label for="auth-login-student-id">Student ID</label>
              <input id="auth-login-student-id" name="student_id" type="text" placeholder="23N101" required>
            </div>
            <div class="auth-field full">
              <label for="auth-login-password">Password</label>
              <input id="auth-login-password" name="password" type="password" placeholder="Your password" required>
            </div>
          </div>
          <div class="auth-status" id="auth-login-status">Enter your login details.</div>
          <div class="auth-actions">
            <button type="button" class="auth-button secondary" id="auth-cancel-btn">Close</button>
            <button type="submit" class="auth-button">Login</button>
          </div>
        </form>
        <form class="auth-form" id="auth-register-form">
          <div class="auth-grid">
            <div class="auth-field">
              <label for="auth-register-student-id">Student ID</label>
              <input id="auth-register-student-id" name="student_id" type="text" placeholder="23N101" required>
            </div>
            <div class="auth-field">
              <label for="auth-register-email">Email</label>
              <input id="auth-register-email" name="email" type="email" placeholder="student@college.edu" required>
            </div>
            <div class="auth-field">
              <label for="auth-register-college-id">College ID</label>
              <input id="auth-register-college-id" name="college_id" type="text" placeholder="COLL-001" required>
            </div>
            <div class="auth-field">
              <label for="auth-register-regulation-id">Regulation ID</label>
              <input id="auth-register-regulation-id" name="regulation_id" type="text" placeholder="2023" required>
            </div>
            <div class="auth-field full">
              <label for="auth-register-password">Password</label>
              <input id="auth-register-password" name="password" type="password" placeholder="Create a password" required>
            </div>
          </div>
          <div class="auth-note">Registration stores the password hash in PostgreSQL and issues the first access + refresh token pair immediately after signup.</div>
          <div class="auth-status" id="auth-register-status">Create your account to continue.</div>
          <div class="auth-actions">
            <button type="button" class="auth-button secondary" id="auth-register-cancel-btn">Close</button>
            <button type="submit" class="auth-button">Create account</button>
          </div>
        </form>
      </div>
    `;
    document.body.appendChild(state.modal);

    state.title = state.modal.querySelector('#auth-title');
    state.subtitle = state.modal.querySelector('#auth-sub');
    state.loginForm = state.modal.querySelector('#auth-login-form');
    state.registerForm = state.modal.querySelector('#auth-register-form');
    state.loginStatus = state.modal.querySelector('#auth-login-status');
    state.registerStatus = state.modal.querySelector('#auth-register-status');

    state.modal.querySelector('#auth-close').addEventListener('click', closeModal);
    state.modal.querySelector('#auth-cancel-btn').addEventListener('click', closeModal);
    state.modal.querySelector('#auth-register-cancel-btn').addEventListener('click', closeModal);
    state.modal.addEventListener('click', (event) => {
      if (event.target === state.modal) {
        closeModal();
      }
    });
    state.modal.querySelectorAll('[data-auth-tab]').forEach((button) => {
      button.addEventListener('click', () => setTab(button.dataset.authTab || 'login'));
    });

    state.loginForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const formData = new FormData(state.loginForm);
      setFormStatus('login', 'Signing in...');
      try {
        await login({
          student_id: String(formData.get('student_id') || '').trim(),
          password: String(formData.get('password') || ''),
        });
        setFormStatus('login', 'Signed in successfully.', 'ok');
      } catch (error) {
        setFormStatus('login', error.message || 'Unable to sign in.', 'bad');
      }
    });

    state.registerForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const formData = new FormData(state.registerForm);
      setFormStatus('register', 'Creating account...');
      try {
        await register({
          student_id: String(formData.get('student_id') || '').trim(),
          email: String(formData.get('email') || '').trim(),
          password: String(formData.get('password') || ''),
          college_id: String(formData.get('college_id') || '').trim(),
          regulation_id: String(formData.get('regulation_id') || '').trim(),
        });
        setFormStatus('register', 'Account created successfully.', 'ok');
      } catch (error) {
        setFormStatus('register', error.message || 'Unable to create account.', 'bad');
      }
    });
  }

  function setTab(tab) {
    state.activeTab = tab === 'register' ? 'register' : 'login';
    if (!state.modal) {
      return;
    }
    state.modal.querySelectorAll('[data-auth-tab]').forEach((button) => {
      button.classList.toggle('active', button.dataset.authTab === state.activeTab);
    });
    state.loginForm.classList.toggle('active', state.activeTab === 'login');
    state.registerForm.classList.toggle('active', state.activeTab === 'register');
  }

  function setFormStatus(formName, message, kind = '') {
    const target = formName === 'login' ? state.loginStatus : state.registerStatus;
    if (!target) {
      return;
    }
    target.textContent = message;
    target.className = `auth-status${kind ? ` ${kind}` : ''}`;
  }

  function openModal(tab = 'login', message = '') {
    buildModal();
    setTab(tab);
    if (message) {
      state.subtitle.textContent = message;
    }
    state.modal.classList.remove('hidden');
  }

  function closeModal() {
    if (state.modal) {
      state.modal.classList.add('hidden');
    }
  }

  function renderShell() {
    const topBar = document.querySelector('.top-bar');
    if (!topBar) {
      return;
    }

    if (!state.shell) {
      state.shell = document.createElement('div');
      state.shell.className = 'auth-shell';
      state.shell.innerHTML = `
        <span class="auth-badge" id="auth-badge">Not signed in</span>
        <button type="button" class="auth-button" id="auth-signin-btn">Sign in</button>
        <button type="button" class="auth-button danger hidden" id="auth-signout-btn">Sign out</button>
      `;
      topBar.appendChild(state.shell);
      state.badge = state.shell.querySelector('#auth-badge');
      state.signinButton = state.shell.querySelector('#auth-signin-btn');
      state.signoutButton = state.shell.querySelector('#auth-signout-btn');
      state.signinButton.addEventListener('click', () => openModal('login'));
      state.signoutButton.addEventListener('click', async () => {
        try {
          await logout();
        } catch (error) {
          clearSession();
          window.location.replace('/');
        }
      });
    }

    const label = state.user?.student_id
      ? `${state.user.student_id}${state.user.email ? ` · ${state.user.email}` : ''}`
      : 'Not signed in';
    state.badge.textContent = label;
    state.signinButton.classList.toggle('hidden', Boolean(state.user?.student_id));
    state.signoutButton.classList.toggle('hidden', !state.user?.student_id);
  }

  function clearSession() {
    writeValue(STORAGE_KEYS.accessToken, '');
    writeValue(STORAGE_KEYS.refreshToken, '');
    writeJson(STORAGE_KEYS.user, null);
    state.user = null;
    renderShell();
  }

  async function request(path, init = {}, options = {}) {
    const includeAuth = options.includeAuth !== false;
    const allowRefresh = options.allowRefresh !== false;
    const skipAutoRefresh = ['/auth/login', '/auth/register', '/auth/logout', '/auth/refresh'].includes(path);
    const headers = new Headers(init.headers || {});

    if (includeAuth) {
      const accessToken = getAccessToken();
      if (accessToken) {
        headers.set('Authorization', `Bearer ${accessToken}`);
      }
    }

    if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json');
    }

    let response = await fetch(`${apiBase}${path}`, {
      ...init,
      headers,
    });

    if (response.status !== 401 || !includeAuth || !allowRefresh || skipAutoRefresh) {
      return response;
    }

    const refreshed = await refreshSession();
    if (!refreshed) {
      openModal('login', 'Your session expired. Please sign in again.');
      return response;
    }

    const retryHeaders = new Headers(init.headers || {});
    const refreshedToken = getAccessToken();
    if (refreshedToken) {
      retryHeaders.set('Authorization', `Bearer ${refreshedToken}`);
    }
    if (init.body && !(init.body instanceof FormData) && !retryHeaders.has('Content-Type')) {
      retryHeaders.set('Content-Type', 'application/json');
    }

    return fetch(`${apiBase}${path}`, {
      ...init,
      headers: retryHeaders,
    });
  }

  async function refreshSession() {
    const refreshToken = getRefreshToken();
    if (!refreshToken) {
      return false;
    }

    const response = await request('/auth/refresh', {
      method: 'POST',
      body: JSON.stringify({ refresh_token: refreshToken }),
    }, { includeAuth: false, allowRefresh: false });

    if (!response.ok) {
      clearSession();
      return false;
    }

    const payload = await response.json();
    setSession(payload, payload.user || null);
    return true;
  }

  async function login(payload) {
    const response = await request('/auth/login', {
      method: 'POST',
      body: JSON.stringify(payload),
    }, { includeAuth: false, allowRefresh: false });

    if (!response.ok) {
      let message = 'Unable to sign in.';
      try {
        const errorPayload = await response.json();
        message = errorPayload.detail || message;
      } catch (error) {
        const text = await response.text();
        if (text) {
          message = text;
        }
      }
      throw new Error(message);
    }

    const data = await response.json();
    setSession(data, data.user || null);
    closeModal();
    window.location.assign(data.has_regulation ? '/app' : '/app/regulation/upload');
    return data;
  }

  async function register(payload) {
    const response = await request('/auth/register', {
      method: 'POST',
      body: JSON.stringify(payload),
    }, { includeAuth: false, allowRefresh: false });

    if (!response.ok) {
      let message = 'Unable to register.';
      try {
        const errorPayload = await response.json();
        message = errorPayload.detail || message;
      } catch (error) {
        const text = await response.text();
        if (text) {
          message = text;
        }
      }
      throw new Error(message);
    }

    await login({ student_id: payload.student_id, password: payload.password });
  }

  async function logout() {
    const refreshToken = getRefreshToken();
    if (refreshToken) {
      await request('/auth/logout', {
        method: 'POST',
        body: JSON.stringify({ refresh_token: refreshToken }),
      }, { includeAuth: false, allowRefresh: false });
    }
    clearSession();
    window.location.replace('/');
  }

  async function hydrateSession() {
    injectStyles();
    buildModal();
    renderShell();
    if (!getAccessToken()) {
      return false;
    }
    const response = await request('/auth/me', {}, { allowRefresh: true, includeAuth: true });
    if (!response.ok) {
      clearSession();
      return false;
    }
    const payload = await response.json();
    setSession({
      access_token: getAccessToken(),
      refresh_token: getRefreshToken(),
      user: payload.student || payload.user || null,
    }, payload.student || payload.user || null);
    return true;
  }

  window.YggdrasilAuth = {
    request,
    login,
    register,
    logout,
    clearSession,
    openModal,
    hydrateSession,
    getAccessToken,
    getRefreshToken,
    hasSession,
  };

  injectStyles();
  renderShell();
  window.addEventListener('storage', () => {
    state.user = readJson(STORAGE_KEYS.user);
    renderShell();
  });
})();