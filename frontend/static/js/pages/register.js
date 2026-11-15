import { apiRequest, setTokens } from '../core/api.js';
import { setStoredUser } from '../core/auth.js';
import { setAlert, setFieldError, showToast } from '../core/ui.js';

const USERNAME_RE = /^[a-zA-Z0-9_]{3,30}$/;
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function initRegisterPage() {
  const form = document.getElementById('register-form');
  if (!form) return;

  const submitBtn = document.getElementById('register-submit-btn');

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    setAlert('register-alert', '');

    const fullName = document.getElementById('reg-full-name').value.trim();
    const username = document.getElementById('reg-username').value.trim();
    const email = document.getElementById('reg-email').value.trim();
    const password = document.getElementById('reg-password').value;
    const passwordConfirm = document.getElementById('reg-password-confirm').value;

    setFieldError('reg-full-name', 'err-reg-full-name', '');
    setFieldError('reg-username', 'err-reg-username', '');
    setFieldError('reg-email', 'err-reg-email', '');
    setFieldError('reg-password', 'err-reg-password', '');
    setFieldError('reg-password-confirm', 'err-reg-password-confirm', '');

    let valid = true;
    if (fullName.length < 2) {
      setFieldError('reg-full-name', 'err-reg-full-name', 'Enter your full name.');
      valid = false;
    }
    if (!USERNAME_RE.test(username)) {
      setFieldError('reg-username', 'err-reg-username', 'Username must be 3–30 letters, numbers, or underscores.');
      valid = false;
    }
    if (!EMAIL_RE.test(email)) {
      setFieldError('reg-email', 'err-reg-email', 'Enter a valid email address.');
      valid = false;
    }
    if (password.length < 8 || !/[A-Za-z]/.test(password) || !/\d/.test(password)) {
      setFieldError('reg-password', 'err-reg-password', 'Password must be at least 8 characters and include letters and numbers.');
      valid = false;
    }
    if (password !== passwordConfirm) {
      setFieldError('reg-password-confirm', 'err-reg-password-confirm', 'Passwords do not match.');
      valid = false;
    }
    if (!valid) return;

    submitBtn.disabled = true;
    submitBtn.textContent = 'Creating account...';

    try {
      const data = await apiRequest('/auth/register/', {
        method: 'POST',
        body: JSON.stringify({
          full_name: fullName,
          username,
          email,
          password,
          password_confirm: passwordConfirm,
        }),
      }, { auth: false });

      setTokens({ access: data.access, refresh: data.refresh });
      setStoredUser(data.user);
      showToast('Account created. Welcome to AVORA.', 'success');
      window.location.replace('/dashboard/');
    } catch (error) {
      setAlert('register-alert', error.message || 'Unable to register account.', 'error');
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = 'Create Account';
    }
  });
}

document.addEventListener('DOMContentLoaded', initRegisterPage);
