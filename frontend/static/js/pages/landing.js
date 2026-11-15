import { extractMeetingCode, fetchCurrentUser } from '../core/auth.js';
import { showToast } from '../core/ui.js';

async function initLandingPage() {
  const user = await fetchCurrentUser();
  if (user) {
    const authLink = document.getElementById('nav-auth-link');
    const primaryCta = document.getElementById('nav-primary-cta');
    const mobileAuth = document.getElementById('mobile-auth-link');
    const mobileCta = document.getElementById('mobile-primary-cta');
    const heroStart = document.getElementById('hero-get-started');

    if (authLink) {
      authLink.textContent = 'Profile';
      authLink.href = '/profile/';
    }
    if (primaryCta) {
      primaryCta.textContent = 'Open Dashboard';
      primaryCta.href = '/dashboard/';
    }
    if (mobileAuth) {
      mobileAuth.textContent = 'Profile';
      mobileAuth.href = '/profile/';
    }
    if (mobileCta) {
      mobileCta.textContent = 'Open Dashboard';
      mobileCta.href = '/dashboard/';
    }
    if (heroStart) {
      heroStart.textContent = 'Go to Dashboard';
      heroStart.href = '/dashboard/';
    }
  }

  const openJoinBtn = document.getElementById('hero-open-join');
  const codeInput = document.getElementById('landing-meeting-code');
  if (openJoinBtn && codeInput) {
    openJoinBtn.addEventListener('click', () => {
      codeInput.focus();
    });
  }

  const joinForm = document.getElementById('landing-join-form');
  if (joinForm && codeInput) {
    joinForm.addEventListener('submit', (event) => {
      event.preventDefault();
      const code = extractMeetingCode(codeInput.value);
      if (!code) {
        showToast('Enter a valid meeting ID or invitation link.', 'warning');
        codeInput.focus();
        return;
      }
      window.location.href = `/meeting/${encodeURIComponent(code)}/`;
    });
  }
}

document.addEventListener('DOMContentLoaded', initLandingPage);
