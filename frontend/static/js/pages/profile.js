import { apiRequest } from '../core/api.js';
import { requireAuth, setStoredUser } from '../core/auth.js';
import { formatDateTime, setAlert, showToast } from '../core/ui.js';

async function initProfilePage() {
  const user = await requireAuth();
  if (!user) return;

  document.getElementById('prof-username').value = user.username;
  document.getElementById('prof-full-name').value = user.full_name || '';
  document.getElementById('prof-email').value = user.email || '';
  document.getElementById('prof-bio').value = user.bio || '';

  const metaLine = document.getElementById('account-meta-line');
  if (metaLine) {
    metaLine.textContent = `Role: ${user.role || 'member'} · Member since ${formatDateTime(user.date_joined)}`;
  }

  const profileForm = document.getElementById('profile-form');
  profileForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    setAlert('profile-alert', '');
    const btn = document.getElementById('save-profile-btn');
    btn.disabled = true;
    btn.textContent = 'Saving...';

    try {
      const updated = await apiRequest('/auth/profile/', {
        method: 'PATCH',
        body: JSON.stringify({
          full_name: document.getElementById('prof-full-name').value.trim(),
          email: document.getElementById('prof-email').value.trim(),
          bio: document.getElementById('prof-bio').value.trim(),
        }),
      });
      setStoredUser(updated);
      document.getElementById('sidebar-user-name').textContent = updated.full_name || updated.username;
      document.getElementById('sidebar-user-email').textContent = updated.email;
      setAlert('profile-alert', 'Profile updated successfully.', 'success');
      showToast('Profile saved.', 'success');
    } catch (err) {
      setAlert('profile-alert', err.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Save Changes';
    }
  });

  const passwordForm = document.getElementById('password-form');
  passwordForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    setAlert('password-alert', '');

    const oldPassword = document.getElementById('old-password').value;
    const newPassword = document.getElementById('new-password').value;
    const confirmPassword = document.getElementById('confirm-new-password').value;

    if (newPassword.length < 8) {
      setAlert('password-alert', 'New password must be at least 8 characters.', 'error');
      return;
    }
    if (newPassword !== confirmPassword) {
      setAlert('password-alert', 'New passwords do not match.', 'error');
      return;
    }

    const btn = document.getElementById('change-password-btn');
    btn.disabled = true;
    btn.textContent = 'Updating...';

    try {
      await apiRequest('/auth/password-change/', {
        method: 'POST',
        body: JSON.stringify({
          old_password: oldPassword,
          new_password: newPassword,
        }),
      });
      passwordForm.reset();
      setAlert('password-alert', 'Password updated successfully.', 'success');
      showToast('Password updated.', 'success');
    } catch (err) {
      setAlert('password-alert', err.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Update Password';
    }
  });
}

document.addEventListener('DOMContentLoaded', initProfilePage);
