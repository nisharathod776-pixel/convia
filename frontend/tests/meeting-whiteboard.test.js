import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { CollaborativeWhiteboard } from '../static/js/meeting/whiteboard.js';

function createContext() {
  const calls = [];
  const state = {
    calls,
    globalCompositeOperation: 'source-over',
    globalAlpha: 1,
    strokeStyle: '#000000',
    lineWidth: 1,
    lineCap: 'butt',
    lineJoin: 'miter',
    shadowColor: 'transparent',
    shadowBlur: 0,
  };
  return new Proxy(state, {
    get(target, key) {
      if (key in target) return target[key];
      return (...args) => calls.push({ method: String(key), args });
    },
    set(target, key, value) {
      target[key] = value;
      calls.push({ method: 'set', property: String(key), value });
      return true;
    },
  });
}

function createWhiteboard(tool) {
  const rect = { left: 20, top: 10, width: 400, height: 200 };
  const ctx = createContext();
  const previewCtx = createContext();
  const emitted = [];
  const whiteboard = Object.create(CollaborativeWhiteboard.prototype);
  Object.assign(whiteboard, {
    canvas: {
      getBoundingClientRect: () => rect,
      focus() {},
      setPointerCapture() {},
    },
    wrap: { getBoundingClientRect: () => rect },
    ctx,
    previewCtx,
    socketClient: {
      emit(event, operation) {
        emitted.push({ event, operation });
        return true;
      },
    },
    tool,
    color: '#F97316',
    size: 8,
    eraserSize: 24,
    enabled: true,
    isDrawing: false,
    activePointerId: null,
    startPoint: null,
    lastPoint: null,
    operations: [],
    seenOpIds: new Set(),
    eraserCursor: { classList: { add() {}, remove() {} } },
  });
  return { whiteboard, ctx, previewCtx, emitted };
}

for (const [tool, previewMethod] of [
  ['rectangle', 'strokeRect'],
  ['ellipse', 'ellipse'],
  ['triangle', 'closePath'],
  ['line', 'lineTo'],
  ['arrow', 'lineTo'],
]) {
  test(`${tool} previews and commits the selected drag area`, () => {
    const { whiteboard, ctx, previewCtx, emitted } = createWhiteboard(tool);
    const pointerType = tool === 'ellipse' ? 'touch' : 'mouse';
    whiteboard.handlePointerDown({ button: 0, pointerId: 7, pointerType, clientX: 70, clientY: 30 });
    whiteboard.handlePointerMove({ pointerId: 7, pointerType, clientX: 270, clientY: 110 });

    assert.ok(previewCtx.calls.some((call) => call.method === previewMethod));

    whiteboard.handlePointerUp({ pointerId: 7, pointerType, clientX: 270, clientY: 110 });
    assert.equal(emitted.length, 1);
    assert.equal(emitted[0].event, 'wb_draw');
    assert.deepEqual(
      [emitted[0].operation.x1, emitted[0].operation.y1, emitted[0].operation.x2, emitted[0].operation.y2],
      [0.125, 0.1, 0.625, 0.5],
    );
    assert.equal(emitted[0].operation.tool, tool);
    assert.equal(emitted[0].operation.color, '#F97316');
    assert.equal(emitted[0].operation.size, 8);
    assert.ok(ctx.calls.some((call) => call.method === previewMethod));

    if (tool === 'rectangle') {
      const rectangle = ctx.calls.find((call) => call.method === 'strokeRect');
      assert.deepEqual(rectangle.args, [50, 20, 200, 80]);
    }
  });
}

test('changing colour affects new shapes without recolouring previous operations', () => {
  const { whiteboard, emitted } = createWhiteboard('rectangle');
  const drag = (pointerId) => {
    whiteboard.handlePointerDown({ button: 0, pointerId, clientX: 70, clientY: 30 });
    whiteboard.handlePointerMove({ pointerId, clientX: 270, clientY: 110 });
    whiteboard.handlePointerUp({ pointerId, clientX: 270, clientY: 110 });
  };

  drag(1);
  whiteboard.color = '#2563EB';
  drag(2);
  assert.equal(emitted[0].operation.color, '#F97316');
  assert.equal(emitted[1].operation.color, '#2563EB');
  assert.equal(whiteboard.operations[0].color, '#F97316');
});

test('normalized shape coordinates scale with the canvas after resizing', () => {
  const { whiteboard, ctx } = createWhiteboard('rectangle');
  const operation = {
    type: 'draw',
    tool: 'rectangle',
    x1: 0.125,
    y1: 0.1,
    x2: 0.625,
    y2: 0.5,
    color: '#16A34A',
    size: 4,
  };
  whiteboard.renderDrawOperation(ctx, operation, 400, 200);
  const original = ctx.calls.find((call) => call.method === 'strokeRect').args;
  ctx.calls.length = 0;
  whiteboard.renderDrawOperation(ctx, operation, 800, 400);
  const resized = ctx.calls.find((call) => call.method === 'strokeRect').args;
  assert.deepEqual(resized, original.map((value) => value * 2));
});

test('the palette contains the nine requested colours and no removed tools', () => {
  const template = readFileSync(new URL('../templates/meeting.html', import.meta.url), 'utf8');
  const colors = [...template.matchAll(/data-wb-color="(#[\da-f]{6})"/gi)].map((match) => match[1].toUpperCase());
  assert.deepEqual(colors, [
    '#000000',
    '#EF4444',
    '#2563EB',
    '#16A34A',
    '#FACC15',
    '#F97316',
    '#8B5CF6',
    '#EC4899',
    '#FFFFFF',
  ]);
  assert.doesNotMatch(template, /data-wb-tool="(?:text|highlighter)"/);
});

test('eraser uses destination-out and its focused shortcuts adjust the displayed size', () => {
  const { whiteboard, ctx } = createWhiteboard('eraser');
  whiteboard.renderDrawOperation(ctx, {
    tool: 'eraser',
    x1: 0.1,
    y1: 0.2,
    x2: 0.4,
    y2: 0.5,
    color: '#000000',
    size: 32,
  }, 400, 200);
  assert.ok(ctx.calls.some((call) => call.property === 'globalCompositeOperation' && call.value === 'destination-out'));
  assert.equal(ctx.lineWidth, 32);

  const oldDocument = globalThis.document;
  const sizeLabel = { textContent: '24px' };
  globalThis.document = {
    activeElement: whiteboard.canvas,
    getElementById: () => sizeLabel,
  };
  try {
    let prevented = false;
    whiteboard.handleEraserShortcut({
      ctrlKey: true,
      key: '+',
      code: 'Equal',
      preventDefault() { prevented = true; },
    });
    assert.equal(whiteboard.eraserSize, 28);
    assert.equal(sizeLabel.textContent, '28px');
    assert.equal(prevented, true);

    whiteboard.handleEraserShortcut({
      ctrlKey: true,
      key: '-',
      code: 'Minus',
      preventDefault() {},
    });
    assert.equal(whiteboard.eraserSize, 24);
    assert.equal(sizeLabel.textContent, '24px');

    globalThis.document.activeElement = {};
    whiteboard.handleEraserShortcut({
      ctrlKey: true,
      key: '-',
      code: 'Minus',
      preventDefault() {},
    });
    assert.equal(whiteboard.eraserSize, 24);
  } finally {
    if (oldDocument === undefined) delete globalThis.document;
    else globalThis.document = oldDocument;
  }
});
