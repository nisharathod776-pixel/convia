export class CollaborativeWhiteboard {
  constructor({ canvasId, wrapId, socketClient }) {
    this.canvas = document.getElementById(canvasId);
    this.wrap = document.getElementById(wrapId);
    this.socketClient = socketClient;
    this.ctx = this.canvas ? this.canvas.getContext('2d') : null;

    this.tool = 'pen';
    this.color = '#0F172A';
    this.size = 4;
    this.isDrawing = false;
    this.lastPoint = null;
    this.operations = [];
    this.seenOpIds = new Set();

    this.bindControls();
    this.resizeCanvas();
    window.addEventListener('resize', () => this.resizeCanvas());
  }

  bindControls() {
    document.querySelectorAll('[data-wb-tool]').forEach((btn) => {
      btn.addEventListener('click', () => {
        this.tool = btn.getAttribute('data-wb-tool');
        document.querySelectorAll('[data-wb-tool]').forEach((b) => b.classList.remove('is-active'));
        btn.classList.add('is-active');
      });
    });

    document.querySelectorAll('[data-wb-color]').forEach((btn) => {
      btn.addEventListener('click', () => {
        this.color = btn.getAttribute('data-wb-color');
        document.querySelectorAll('[data-wb-color]').forEach((b) => b.classList.remove('is-active'));
        btn.classList.add('is-active');
      });
    });

    const sizeSelect = document.getElementById('wb-brush-size');
    sizeSelect?.addEventListener('change', (e) => {
      this.size = Number(e.target.value) || 4;
    });

    document.getElementById('wb-clear-btn')?.addEventListener('click', () => {
      this.clearLocal();
      this.socketClient.emit('wb_clear', {});
    });

    if (!this.canvas) return;

    this.canvas.addEventListener('pointerdown', (e) => this.handlePointerDown(e));
    this.canvas.addEventListener('pointermove', (e) => this.handlePointerMove(e));
    window.addEventListener('pointerup', () => {
      this.isDrawing = false;
      this.lastPoint = null;
    });
  }

  resizeCanvas() {
    if (!this.canvas || !this.wrap || !this.ctx) return;
    const rect = this.wrap.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;

    const dpr = window.devicePixelRatio || 1;
    this.canvas.width = Math.floor(rect.width * dpr);
    this.canvas.height = Math.floor(rect.height * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
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
    const pt = this.getNormalizedPoint(event);
    if (this.tool === 'text') {
      this.spawnTextInput(pt);
      return;
    }
    this.isDrawing = true;
    this.lastPoint = pt;
    this.drawSegmentAndSync(pt, pt);
  }

  handlePointerMove(event) {
    if (!this.isDrawing || !this.lastPoint) return;
    const pt = this.getNormalizedPoint(event);
    this.drawSegmentAndSync(this.lastPoint, pt);
    this.lastPoint = pt;
  }

  drawSegmentAndSync(fromPt, toPt) {
    const strokeColor = this.tool === 'eraser' ? '#FFFFFF' : this.color;
    const strokeSize = this.tool === 'eraser' ? Math.max(12, this.size * 3) : this.size;
    const op = {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      type: 'draw',
      x1: fromPt.x,
      y1: fromPt.y,
      x2: toPt.x,
      y2: toPt.y,
      color: strokeColor,
      size: strokeSize,
    };
    this.applyOperation(op);
    this.socketClient.emit('wb_draw', op);
  }

  spawnTextInput(pt) {
    const existing = this.wrap.querySelector('.wb-text-input');
    if (existing) existing.remove();

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'wb-text-input';
    input.placeholder = 'Type text & press Enter';
    input.style.left = `${pt.px}px`;
    input.style.top = `${pt.py}px`;
    input.style.color = this.color;

    let committed = false;
    const commit = () => {
      if (committed) return;
      committed = true;
      const text = input.value.trim();
      input.remove();
      if (!text) return;
      const op = {
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        type: 'text',
        x: pt.x,
        y: pt.y,
        text,
        color: this.color,
        size: Math.max(14, this.size * 3 + 6),
      };
      this.applyOperation(op);
      this.socketClient.emit('wb_text', op);
    };

    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') commit();
      if (e.key === 'Escape') {
        committed = true;
        input.remove();
      }
    });
    input.addEventListener('blur', commit);

    this.wrap.appendChild(input);
    input.focus();
  }

  applyOperation(op, record = true) {
    if (!op) return;
    if (op.id && this.seenOpIds.has(op.id)) return;
    if (op.id) this.seenOpIds.add(op.id);
    if (record) this.operations.push(op);
    this.renderOp(op);
  }

  renderOp(op) {
    if (!this.ctx || !this.canvas) return;
    const rect = this.wrap.getBoundingClientRect();
    const w = rect.width;
    const h = rect.height;
    if (!w || !h) return;

    if (op.type === 'draw') {
      this.ctx.save();
      this.ctx.strokeStyle = op.color || '#0F172A';
      this.ctx.lineWidth = Number(op.size) || 4;
      this.ctx.lineCap = 'round';
      this.ctx.lineJoin = 'round';
      this.ctx.beginPath();
      this.ctx.moveTo(op.x1 * w, op.y1 * h);
      this.ctx.lineTo(op.x2 * w, op.y2 * h);
      this.ctx.stroke();
      this.ctx.restore();
    } else if (op.type === 'text') {
      this.ctx.save();
      this.ctx.fillStyle = op.color || '#0F172A';
      this.ctx.font = `600 ${Number(op.size) || 18}px "DM Sans", sans-serif`;
      this.ctx.textBaseline = 'top';
      this.ctx.fillText(op.text || '', op.x * w, op.y * h);
      this.ctx.restore();
    }
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
    if (this.ctx && this.wrap) {
      const rect = this.wrap.getBoundingClientRect();
      this.ctx.clearRect(0, 0, rect.width, rect.height);
    }
  }

  redrawAll() {
    if (!this.ctx || !this.wrap) return;
    const rect = this.wrap.getBoundingClientRect();
    this.ctx.clearRect(0, 0, rect.width, rect.height);
    for (const op of this.operations) {
      this.renderOp(op);
    }
  }
}
