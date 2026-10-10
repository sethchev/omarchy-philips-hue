// Execute the panel's actual JS functions with mocked process/timer boundaries.
// No Quickshell session, credential file, or bridge connection is required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const dir = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(dir, 'Panel.qml'), 'utf8');

function harness() {
  const later = [];
  const timers = () => ({ count: 0, restart() { this.count++; }, stop() {} });
  const ctx = {
    Qt: { resolvedUrl: p => ({toString: () => 'file://' + p}), callLater: fn => later.push(fn) },
    stateProc: { running: false }, actionProc: { running: false },
    pollTimer: timers(), resyncTimer: timers(), sceneStatusTimer: timers(),
    configFile: { reload() {} }, themeConfigFile: { count: 0, reload() { this.count++; } },
    root: {
      config: {apiVersion: 'v2'}, lightsById: {}, allControlId: '', rooms: [], scenes: [],
      roomsWithLights: [], orphanLights: [], pickerOpenById: {}, favoriteRooms: {},
      actionQueue: [], currentAction: null, stateRevision: 0, pendingFetches: 0,
      fetchScheduled: false, refreshRequested: false, loading: false,
      lastFetchFailed: false, consecutiveFetchFailures: 0,
      lastSuccessfulFetch: 'previous', sceneStatus: '', sceneStatusRoomId: '',
      actionError: '', opened: true
    }
  };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(dir, 'HueApi.js'), 'utf8'), ctx);
  ctx.HueApi = {};
  for (const key of Object.keys(ctx)) if (typeof ctx[key] === 'function') ctx.HueApi[key] = ctx[key];
  const functions = [...source.matchAll(/^  function (\w+)\([^]*?^  }/gm)];
  assert.ok(functions.length > 30, 'Load the actual panel functions');
  for (const match of functions) {
    vm.runInContext(match[0], ctx);
    ctx.root[match[1]] = ctx[match[1]];
  }
  Object.defineProperty(ctx.root, 'lightTotal', {get: () => ctx.root.lightsTotal()});
  ctx.flush = () => { while (later.length) later.shift()(); };
  return ctx;
}
function snapshot() {
  return JSON.stringify({
    lights: {
      1: {api_id: 'uuid1', name: 'Desk', state: {on: true, reachable: false, bri: 127}},
      2: {api_id: 'uuid2', name: 'White', state: {on: false, bri: 127}},
      3: {api_id: 'uuid3', name: 'Orphan', state: {on: false}}
    },
    groups: {
      4: {type: 'Room', name: 'Office', lights: ['1', '2'], state: {any_on: true}, control_id: 'group4'},
      5: {type: 'Zone', name: 'Downstairs', lights: ['1'], state: {any_on: true}, control_id: 'group5'}
    }, scenes: {}
  });
}
function load(ctx) {
  const parsed = ctx.HueApi.parseSnapshot(snapshot());
  Object.assign(ctx.root, {lightsById: parsed.lightsById, rooms: parsed.rooms, scenes: parsed.scenes});
  ctx.root.assembleRooms();
}
function completeAction(ctx, code = 0, error = '') {
  ctx.actionProc.running = false;
  Object.assign(ctx.actionProc, {actionExited: true, actionOutputFinished: true, actionExitCode: code, errorOutput: error});
  ctx.root.tryCompleteAction();
  ctx.flush();
}

test('master toggle counts unique lights, includes orphans, and requires every bulb on', () => {
  const h = harness(); load(h);
  assert.equal(h.root.lightsTotal(), 3);
  assert.equal(h.root.unreachableLightCount(), 1);
  assert.equal(h.root.computeAllLightsOn(), false);
  h.root.toggleAll(true);
  assert.equal(h.root.computeAllLightsOn(), true);
  assert.equal(h.actionProc.command[2], 'put-all');
  assert.deepEqual(JSON.parse(h.actionProc.command[3]), ['uuid1', 'uuid2', 'uuid3']);
  assert.equal(h.root.orphanLights[0].on, true);
});

test('optimistic updates retain reachability and update overlapping rooms', () => {
  const h = harness(); load(h);
  h.root.setLightOn('1', false);
  assert.equal(h.root.lightsById['1'].reachable, false);
  assert.equal(h.root.roomById('4').on, false);
  assert.equal(h.root.roomById('5').on, false);
  h.root.setRoomOn('4', true);
  assert.equal(h.root.roomById('5').on, true);
});

test('master toggle uses the bridge-wide service when available', () => {
  const h = harness(); load(h);
  h.root.allControlId = 'bridge-service';
  h.root.toggleAll(false);
  assert.equal(h.actionProc.command[2], 'put-group');
  assert.equal(h.actionProc.command[3], 'bridge-service');
  assert.equal(h.root.orphanLights[0].on, false);
});

test('refresh waits for exit and stdout, commits all resources, keeps picker state', () => {
  const h = harness(); load(h);
  h.root.toggleColorPicker('1');
  h.root.refresh(); h.flush();
  assert.equal(h.stateProc.command[2], 'get-state');
  Object.assign(h.stateProc, {fetchOutput: snapshot(), fetchOutputFinished: true});
  h.root.tryCompleteFetch(h.stateProc);
  assert.equal(h.root.pendingFetches, 1);
  Object.assign(h.stateProc, {fetchExited: true, fetchExitCode: 0});
  h.root.tryCompleteFetch(h.stateProc);
  assert.equal(h.root.pendingFetches, 0);
  assert.equal(h.root.lastFetchFailed, false);
  assert.equal(h.root.pickerOpenById['1'], true);
});

test('malformed or incomplete snapshot retains the entire last good state', () => {
  for (const bad of ['{}', '{"lights":{},"groups":[] ,"scenes":{}}', '{"lights":{"1":null},"groups":{},"scenes":{}}', 'not JSON']) {
    const h = harness(); load(h);
    const previous = h.root.lightsById, rooms = h.root.rooms, scenes = h.root.scenes;
    h.root.refresh(); h.flush();
    Object.assign(h.stateProc, {fetchOutput: bad, fetchOutputFinished: true, fetchExited: true, fetchExitCode: 0});
    h.root.tryCompleteFetch(h.stateProc);
    assert.equal(h.root.lastFetchFailed, true);
    assert.equal(h.root.lastSuccessfulFetch, 'previous');
    assert.equal(h.root.lightsById, previous);
    assert.equal(h.root.rooms, rooms);
    assert.equal(h.root.scenes, scenes);
  }
});

test('late fetch cannot revert a newer optimistic action', () => {
  const h = harness(); load(h);
  h.root.refresh(); h.flush();
  h.root.toggleLight('2', true);
  Object.assign(h.stateProc, {fetchOutput: snapshot(), fetchOutputFinished: true, fetchExited: true, fetchExitCode: 0});
  h.root.tryCompleteFetch(h.stateProc);
  assert.equal(h.root.lightsById['2'].on, true);
  assert.equal(h.root.refreshRequested, true);
  assert.equal(h.root.pendingFetches, 0);
});

test('actions serialize settings writes before sync and defer refresh until drained', () => {
  const h = harness(); load(h);
  h.root.runAction(['python3', 'hue-api.py', 'put-light', 'uuid1', '{}']);
  const first = h.actionProc.command;
  h.root.runAction(['python3', 'hue-api.py', 'write-theme-config', '{}']);
  h.root.runAction(['python3', 'hue-api.py', 'sync-room', '4']);
  h.root.refresh();
  assert.equal(h.actionProc.command, first);
  assert.equal(h.stateProc.running, false);
  completeAction(h);
  assert.equal(h.actionProc.command[2], 'write-theme-config');
  completeAction(h);
  assert.equal(h.actionProc.command[2], 'sync-room');
  completeAction(h);
  assert.equal(h.root.currentAction, null);
  assert.equal(h.resyncTimer.count, 1);
  // Settings toggles must not bypass the tested queue.
  assert.equal((source.match(/actionProc.command\s*=/g) || []).length, 1);
});

test('action failures report stderr and reconcile settings; scenes wait for completion', () => {
  const h = harness(); load(h);
  h.root.applyScene('4', 'Relax');
  assert.equal(h.sceneStatusTimer.count, 0);
  h.actionProc.actionExited = true;
  h.actionProc.actionExitCode = 1;
  h.root.tryCompleteAction();
  assert.notEqual(h.root.currentAction, null);
  h.actionProc.errorOutput = 'Hue error: unauthorized\n';
  h.actionProc.actionOutputFinished = true;
  h.root.tryCompleteAction(); h.flush();
  assert.equal(h.root.actionError, 'Hue error: unauthorized');
  assert.equal(h.root.sceneStatus, 'Could not apply Relax');
  assert.equal(h.themeConfigFile.count, 1);
  assert.equal(h.sceneStatusTimer.count, 1);
  assert.equal(h.resyncTimer.count, 1);
});

test('successful scenes show their result and duplicate completion is ignored', () => {
  const h = harness(); load(h);
  h.root.applyScene('4', 'Read');
  completeAction(h);
  h.root.tryCompleteAction(); h.flush();
  assert.equal(h.root.sceneStatus, 'Applied Read');
  assert.equal(h.sceneStatusTimer.count, 1);
  assert.equal(h.resyncTimer.count, 1);
});

test('two identical scene requests keep the latest pending until its completion', () => {
  const h = harness(); load(h);
  h.root.applyScene('4', 'Read');
  h.root.applyScene('4', 'Read');
  completeAction(h);
  assert.equal(h.root.sceneStatus, 'Applying Read…');
  assert.equal(h.sceneStatusTimer.count, 0);
  completeAction(h);
  assert.equal(h.root.sceneStatus, 'Applied Read');
  assert.equal(h.sceneStatusTimer.count, 1);
});

test('action failure triggers a refresh that replaces optimistic state', () => {
  const h = harness(); load(h);
  h.root.toggleLight('2', true);
  assert.equal(h.root.lightsById['2'].on, true);
  completeAction(h, 1, 'Hue error: failed');
  h.root.refresh(); h.flush();
  Object.assign(h.stateProc, {fetchOutput: snapshot(), fetchOutputFinished: true, fetchExited: true, fetchExitCode: 0});
  h.root.tryCompleteFetch(h.stateProc);
  assert.equal(h.root.lightsById['2'].on, false);
  assert.equal(h.root.actionError, 'Hue error: failed');
});
