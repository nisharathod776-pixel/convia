const SHAPE_TOOLS = new Set(['line', 'rectangle', 'ellipse', 'triangle', 'arrow']);
const DRAW_TOOLS = new Set(['pen', 'eraser', ...SHAPE_TOOLS]);
const RENDER_TOOLS = new Set([...DRAW_TOOLS, 'highlighter']);

export class CollaborativeWhiteboard {
  constructor({ canvasId, wrapId, socketClient }) {
    this.canvas = document.getElementById(canvasId);
    this.previewCanvas = document.getElementById('whiteboard-preview');
    this.wrap = document.getElementById(wrapId);
    this.socketClient = socketClient;
    this.ctx = this.canvas ? this.canvas.getContext('2d') : null;
    this.previewCtx = this.previewCanvas ? this.previewCanvas.getContext('2d') : null;

    this.tool = 'pen';
    this.color = '#000000';
    this.size = 4;
    this.eraserSize = 24;
    this.enabled = false;
    this.isDrawing = false;
    this.activePointerId = null;
    this.startPoint = null;
    this.lastPoint = null;
    this.operations = [];
    this.seenOpIds = new Set();
    this.eraserCursor = document.createElement('div');
    this.eraserCursor.className = 'wb-eraser-cursor hidden';
    this.eraserCursor.setAttribute('aria-hidden', 'true');

    if (this.wrap) this.wrap.appendChild(this.eraserCursor);
    this.bindControls();
    this.resizeCanvas();
    this.setEnabled(false);

    if (typeof ResizeObserver !== 'undefined' && this.wrap) {
      this.resizeObserver = new ResizeObserver(() => this.resizeCanvas());
      this.resizeObserver.observe(this.wrap);
    } else {
      window.addEventListener('resize', () => this.resizeCanvas());
    }
  }

  bindControls() {
    document.querySelectorAll('[data-wb-tool]').forEach((btn) => {
      btn.addEventListener('click', () => {
        this.tool = btn.getAttribute('data-wb-tool');
        document.querySelectorAll('[data-wb-tool]').forEach((button) => {
          const active = button === btn;
          button.classList.toggle('is-active', active);
          button.setAttribute('aria-pressed', String(active));
        });
        document.getElementById('wb-eraser-controls')?.classList.toggle('hidden', this.tool !== 'eraser');
        this.updateEraserCursor();
      });
    });

    document.querySelectorAll('[data-wb-color]').forEach((btn) => {
      btn.addEventListener('click', () => {
        this.color = btn.getAttribute('data-wb-color');
        document.querySelectorAll('[data-wb-color]').forEach((button) => {
          const active = button === btn;
          button.classList.toggle('is-active', active);
          button.setAttribute('aria-pressed', String(active));
        });
      });
    });

    document.getElementById('wb-brush-size')?.addEventListener('change', (event) => {
      const size = Number(event.target.value);
      if (Number.isFinite(size)) this.size = Math.max(1, Math.min(60, size));
    });

    document.getElementById('wb-eraser-smaller')?.addEventListener('click', () => this.adjustEraserSize(-4));
    document.getElementById('wb-eraser-larger')?.addEventListener('click', () => this.adjustEraserSize(4));
    document.getElementById('wb-clear-btn')?.addEventListener('click', () => {
      if (!this.enabled || !this.socketClient.emit('wb_clear', {})) return;
      this.clearLocal();
    });

    if (!this.canvas) return;

    this.canvas.addEventListener('pointerdown', (event) => this.handlePointerDown(event));
    this.canvas.addEventListener('pointermove', (event) => this.handlePointerMove(event));
    this.canvas.addEventListener('pointerup', (event) => this.handlePointerUp(event));
    this.canvas.addEventListener('pointercancel', (event) => {
      if (event.pointerId === this.activePointerId) this.cancelDrawing();
    });
    this.canvas.addEventListener('pointerleave', () => {
      if (!this.isDrawing) this.eraserCursor.classList.add('hidden');
    });
    document.addEventListener('keydown', (event) => this.handleEraserShortcut(event));
  }

  setEnabled(enabled) {
    this.enabled = Boolean(enabled);
    if (this.wrap) this.wrap.classList.toggle('is-offline', !this.enabled);
    document.getElementById('wb-offline-message')?.classList.toggle('hidden', this.enabled);
    document.getElementById('wb-clear-btn')?.toggleAttribute('disabled', !this.enabled);
    this.canvas?.setAttribute('aria-disabled', String(!this.enabled));
    if (!this.enabled) {
      this.cancelDrawing();
      this.eraserCursor.classList.add('hidden');
    }
  }

  adjustEraserSize(delta) {
    this.eraserSize = Math.max(4, Math.min(80, this.eraserSize + delta));
    const label = document.getElementById('wb-eraser-size-value');
    if (label) label.textContent = `${this.eraserSize}px`;
    this.updateEraserCursor();
  }

  handleEraserShortcut(event) {
    if (!this.enabled || this.tool !== 'eraser' || document.activeElement !== this.canvas || !event.ctrlKey) return;
    if (event.key === '-' || event.code === 'NumpadSubtract') {
      event.preventDefault();
      this.adjustEraserSize(-4);
    } else if (event.key === '+' || event.key === '=' || event.code === 'NumpadAdd') {
      event.preventDefault();
      this.adjustEraserSize(4);
    }
  }

  resizeCanvas() {
    if (!this.canvas || !this.wrap || !this.ctx || !this.previewCanvas || !this.previewCtx) return;
    const rect = this.wrap.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;

    const dpr = window.devicePixelRatio || 1;
    [this.canvas, this.previewCanvas].forEach((canvas) => {
      canvas.width = Math.floor(rect.width * dpr);
      canvas.height = Math.floor(rect.height * dpr);
      const context = canvas === this.canvas ? this.ctx : this.previewCtx;
      context.setTransform(dpr, 0, 0, dpr, 0, 0);
    });
    this.redrawAll();
  }

  getNormalizedPoint(event) {
    const rect = this.canvas.getBoundingClientRect();
    return {
      x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
      y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)),
      px: event.clientX - rect.left,
      py: event.clientY - rect.top,
    };
  }

  handlePointerDown(event) {
    if (!this.enabled || event.button !== 0) return;
    const point = this.getNormalizedPoint(event);
    if (!DRAW_TOOLS.has(this.tool)) return;

    this.canvas.focus({ preventScroll: true });
    this.canvas.setPointerCapture(event.pointerId);
    this.activePointerId = event.pointerId;
    this.isDrawing = true;
    this.startPoint = point;
    this.lastPoint = point;
    if (SHAPE_TOOLS.has(this.tool)) {
      this.drawPreview(point);
    } else {
      this.drawSegmentAndSync(point, point);
    }
    this.updateEraserCursor(event);
  }

  handlePointerMove(event) {
    this.updateEraserCursor(event);
    if (!this.enabled || !this.isDrawing || !this.lastPoint || event.pointerId !== this.activePointerId) return;
    const point = this.getNormalizedPoint(event);
    if (SHAPE_TOOLS.has(this.tool)) {
      this.drawPreview(point);
      return;
    }
    this.drawSegmentAndSync(this.lastPoint, point);
    this.lastPoint = point;
  }

  handlePointerUp(event) {
    if (!this.isDrawing || event.pointerId !== this.activePointerId) return;
    const endPoint = this.getNormalizedPoint(event);
    if (SHAPE_TOOLS.has(this.tool) && this.startPoint) {
      this.sendOperation('wb_draw', {
        id: this.createOperationId(),
        type: 'draw',
        tool: this.tool,
        x1: this.startPoint.x,
        y1: this.startPoint.y,
        x2: endPoint.x,
        y2: endPoint.y,
        color: this.color,
        size: this.size,
      });
    }
    this.cancelDrawing();
  }

  cancelDrawing() {
    this.isDrawing = false;
    this.activePointerId = null;
    this.startPoint = null;
    this.lastPoint = null;
    this.clearPreview();
  }

  drawSegmentAndSync(fromPoint, toPoint) {
    const op = {
      id: this.createOperationId(),
      type: 'draw',
      tool: this.tool,
      x1: fromPoint.x,
      y1: fromPoint.y,
      x2: toPoint.x,
      y2: toPoint.y,
      color: this.color,
      size: this.tool === 'eraser' ? this.eraserSize : this.size,
    };
    this.sendOperation('wb_draw', op);
  }

  sendOperation(event, op) {
    if (!this.socketClient.emit(event, op)) {
      this.setEnabled(false);
      return;
    }
    this.applyOperation(op);
  }

  createOperationId() {
    return `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
  }

  updateEraserCursor(event) {
    if (!this.eraserCursor) return;
    if (!this.enabled || this.tool !== 'eraser' || !event) {
      if (this.tool !== 'eraser' || !this.enabled) this.eraserCursor.classList.add('hidden');
      return;
    }
    const rect = this.wrap.getBoundingClientRect();
    this.eraserCursor.style.left = `${event.clientX - rect.left}px`;
    this.eraserCursor.style.top = `${event.clientY - rect.top}px`;
    this.eraserCursor.style.width = `${this.eraserSize}px`;
    this.eraserCursor.style.height = `${this.eraserSize}px`;
    this.eraserCursor.classList.remove('hidden');
  }

  applyOperation(op, record = true) {
    if (!op) return;
    if (op.id && this.seenOpIds.has(op.id)) return;
    if (op.id) this.seenOpIds.add(op.id);
    if (record) this.operations.push(op);
    this.renderOp(op);
  }

  renderOp(op) {
    if (!this.ctx || !this.wrap) return;
    const rect = this.wrap.getBoundingClientRect();
    if (!rect.width || !rect.height) return;

    if (op.type === 'draw') {
      this.renderDrawOperation(this.ctx, op, rect.width, rect.height);
    } else if (op.type === 'text') {
      this.ctx.save();
      this.ctx.fillStyle = op.color || '#0F172A';
      this.ctx.font = `600 ${Number(op.size) || 18}px "DM Sans", sans-serif`;
      this.ctx.textBaseline = 'top';
      this.ctx.fillText(op.text || '', op.x * rect.width, op.y * rect.height);
      this.ctx.restore();
    }
  }

  renderDrawOperation(context, op, width, height) {
    const tool = RENDER_TOOLS.has(op.tool) ? op.tool : 'pen';
    const x1 = op.x1 * width;
    const y1 = op.y1 * height;
    const x2 = op.x2 * width;
    const y2 = op.y2 * height;
    context.save();
    context.globalCompositeOperation = tool === 'eraser' ? 'destination-out' : 'source-over';
    context.globalAlpha = tool === 'highlighter' ? 0.25 : 1;
    context.strokeStyle = op.color || '#0F172A';
    context.lineWidth = Math.max(1, Number(op.size) || 4);
    context.lineCap = 'round';
    context.lineJoin = 'round';
    if (op.color?.toUpperCase() === '#FFFFFF' && tool !== 'eraser') {
      context.shadowColor = '#64748B';
      context.shadowBlur = Math.max(1, context.lineWidth / 2);
    }
    context.beginPath();

    if (tool === 'rectangle') {
      context.strokeRect(x1, y1, x2 - x1, y2 - y1);
    } else if (tool === 'ellipse') {
      context.ellipse((x1 + x2) / 2, (y1 + y2) / 2, Math.abs(x2 - x1) / 2, Math.abs(y2 - y1) / 2, 0, 0, Math.PI * 2);
      context.stroke();
    } else if (tool === 'triangle') {
      const left = Math.min(x1, x2);
      const right = Math.max(x1, x2);
      const top = Math.min(y1, y2);
      const bottom = Math.max(y1, y2);
      context.moveTo((left + right) / 2, top);
      context.lineTo(right, bottom);
      context.lineTo(left, bottom);
      context.closePath();
      context.stroke();
    } else if (tool === 'arrow') {
      const angle = Math.atan2(y2 - y1, x2 - x1);
      const headLength = Math.max(10, context.lineWidth * 3);
      context.moveTo(x1, y1);
      context.lineTo(x2, y2);
      context.moveTo(x2, y2);
      context.lineTo(x2 - headLength * Math.cos(angle - Math.PI / 6), y2 - headLength * Math.sin(angle - Math.PI / 6));
      context.moveTo(x2, y2);
      context.lineTo(x2 - headLength * Math.cos(angle + Math.PI / 6), y2 - headLength * Math.sin(angle + Math.PI / 6));
      context.stroke();
    } else {
      context.moveTo(x1, y1);
      context.lineTo(x2, y2);
      context.stroke();
    }
    context.restore();
  }

  drawPreview(point) {
    if (!this.previewCtx || !this.startPoint || !this.wrap) return;
    this.clearPreview();
    const rect = this.wrap.getBoundingClientRect();
    this.renderDrawOperation(this.previewCtx, {
      type: 'draw',
      tool: this.tool,
      x1: this.startPoint.x,
      y1: this.startPoint.y,
      x2: point.x,
      y2: point.y,
      color: this.color,
      size: this.size,
    }, rect.width, rect.height);
  }

  clearPreview() {
    if (!this.previewCtx || !this.wrap) return;
    const rect = this.wrap.getBoundingClientRect();
    this.previewCtx.clearRect(0, 0, rect.width, rect.height);
  }

  loadState(operations = []) {
    this.operations = [];
    this.seenOpIds.clear();
    for (const op of operations) {
      if (op.id) this.seenOpIds.add(op.id);
      this.operations.push(op);
    }
    this.redrawAll();
  }

  clearLocal() {
    this.operations = [];
    this.seenOpIds.clear();
    this.redrawAll();
    this.clearPreview();
  }

  redrawAll() {
    if (!this.ctx || !this.wrap) return;
    const rect = this.wrap.getBoundingClientRect();
    this.ctx.clearRect(0, 0, rect.width, rect.height);
    for (const op of this.operations) this.renderOp(op);
  }
}
