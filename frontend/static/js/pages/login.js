import { apiRequest, setTokens } from '../core/api.js';
import { setStoredUser } from '../core/auth.js';
import { setAlert, setFieldError, showToast } from '../core/ui.js';

function initLoginPage() {
  const form = document.getElementById('login-form');
  if (!form) return;

  const submitBtn = document.getElementById('login-submit-btn');
  const params = new URLSearchParams(window.location.search);
  const nextUrl = params.get('next');

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    setAlert('login-alert', '');
    setFieldError('login-identifier', 'err-login-identifier', '');
    setFieldError('login-password', 'err-login-password', '');

    const identifier = document.getElementById('login-identifier').value.trim();
    const password = document.getElementById('login-password').value;

    let valid = true;
    if (!identifier) {
      setFieldError('login-identifier', 'err-login-identifier', 'Enter your username or email.');
      valid = false;
    }
    if (!password) {
      setFieldError('login-password', 'err-login-password', 'Enter your password.');
      valid = false;
    }
    if (!valid) return;

    submitBtn.disabled = true;
    submitBtn.textContent = 'Signing in...';

    try {
      const data = await apiRequest('/auth/login/', {
        method: 'POST',
        body: JSON.stringify({ identifier, password }),
      }, { auth: false });

      setTokens({ access: data.access, refresh: data.refresh });
      setStoredUser(data.user);
      showToast('Signed in successfully.', 'success');
      window.location.replace(nextUrl && nextUrl.startsWith('/') ? nextUrl : '/dashboard/');
    } catch (error) {
      setAlert('login-alert', error.message || 'Invalid credentials.', 'error');
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = 'Sign In';
    }
  });
}

document.addEventListener('DOMContentLoaded', initLoginPage);
