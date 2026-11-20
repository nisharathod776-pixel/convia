import { cpSync, mkdirSync, readFileSync } from 'node:fs';
import { basename, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig, type Plugin } from 'vite';

const projectRoot = fileURLToPath(new URL('.', import.meta.url));
const frontendRoot = resolve(projectRoot, 'frontend');
const outputDir = resolve(projectRoot, 'dist');
const localBackend = process.env.CONVIA_DEV_BACKEND_URL || 'http://127.0.0.1:8000';

const templateRoot = resolve(frontendRoot, 'templates');
const plannedPages = {
  landing: resolve(templateRoot, 'landing.html'),
  login: resolve(templateRoot, 'login.html'),
  register: resolve(templateRoot, 'register.html'),
  dashboard: resolve(templateRoot, 'dashboard.html'),
  profile: resolve(templateRoot, 'profile.html'),
  admin: resolve(templateRoot, 'admin.html'),
  meeting: resolve(templateRoot, 'meeting.html'),
};
const plannedPageNames = new Set(Object.values(plannedPages).map((page) => basename(page)));

const pageRoutes = {
  '/': '/templates/landing.html',
  '/login': '/templates/login.html',
  '/register': '/templates/register.html',
  '/dashboard': '/templates/dashboard.html',
  '/profile': '/templates/profile.html',
  '/admin-panel': '/templates/admin.html',
};

function renderDjangoTemplate(pageHtml: string) {
  const blocks = new Map(
    [...pageHtml.matchAll(/{%\s*block\s+(\w+)\s*%}([\s\S]*?){%\s*endblock\s*%}/g)]
      .map((match) => [match[1], match[2]]),
  );
  const baseHtml = readFileSync(resolve(templateRoot, 'base.html'), 'utf8');

  return baseHtml
    .replace(/{%\s*block\s+(\w+)\s*%}([\s\S]*?){%\s*endblock\s*%}/g, (_match, name, fallback) => blocks.get(name) ?? fallback)
    .replace(/{%\s*load\s+static\s*%}/g, '')
    .replace(/{%\s*extends\s+["']base\.html["']\s*%}/g, '')
    .replace(/{%\s*static\s+["']([^"']+)["']\s*%}/g, '/static/$1')
    .replace(/\{\{\s*meeting_code\|default:''\s*\}\}/g, '');
}

const renderExistingDjangoTemplates: Plugin = {
  name: 'render-existing-django-templates-for-static-hosting',
  transformIndexHtml: {
    order: 'pre',
    handler(html, context) {
      const pageName = basename(context.filename);
      if (pageName === 'base.html' || !plannedPageNames.has(pageName)) {
        return html;
      }
      return renderDjangoTemplate(html);
    },
  },
  configureServer(server) {
    server.middlewares.use((request, _response, next) => {
      const requestUrl = new URL(request.url || '/', 'http://localhost');
      const normalizedPath = requestUrl.pathname.replace(/\/$/, '') || '/';
      const meetingMatch = normalizedPath.match(/^\/meeting\/([^/]+)$/);
      const templatePath = meetingMatch
        ? '/templates/meeting.html'
        : Object.hasOwn(pageRoutes, normalizedPath)
          ? pageRoutes[normalizedPath as keyof typeof pageRoutes]
          : undefined;

      if (templatePath) {
        request.url = `${templatePath}${requestUrl.search}`;
      }
      next();
    });
  },
};

const copyDjangoStaticAssets: Plugin = {
  name: 'copy-django-static-assets',
  apply: 'build',
  closeBundle() {
    mkdirSync(outputDir, { recursive: true });
    cpSync(resolve(frontendRoot, 'static'), resolve(outputDir, 'static'), { recursive: true });
  },
};

export default defineConfig({
  root: frontendRoot,
  base: '/',
  publicDir: false,
  plugins: [renderExistingDjangoTemplates, copyDjangoStaticAssets],
  resolve: {
    alias: {
      '@': projectRoot,
    },
  },
  server: {
    port: 3000,
    host: '0.0.0.0',
    proxy: {
      '/api/': {
        target: localBackend,
        changeOrigin: true,
      },
      '/socket.io/': {
        target: localBackend,
        changeOrigin: true,
        ws: true,
      },
    },
  },
  build: {
    outDir: outputDir,
    emptyOutDir: true,
    rollupOptions: {
      input: plannedPages,
    },
  },
});
