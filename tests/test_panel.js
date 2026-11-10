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
  const timers = () => ({ count: 0, running: false,
    start() { this.count++; this.running = true; },
    restart() { this.count++; this.running = true; }, stop() { this.running = false; } });
  const ctx = {
    Qt: { resolvedUrl: p => ({toString: () => 'file://' + p}), callLater: fn => later.push(fn) },
    stateProc: { running: false }, actionProc: { running: false },
    pollTimer: timers(), resyncTimer: timers(), sceneStatusTimer: timers(),
    musicOptionsTimer: timers(),
    configFile: { reload() {} }, themeConfigFile: { count: 0, reload() { this.count++; } },
    root: {
      config: {apiVersion: 'v2'}, lightsById: {}, allControlId: '', rooms: [], scenes: [],
      roomsWithLights: [], orphanLights: [], pickerOpenById: {}, favoriteRooms: {},
      maxQueuedActions: 16, actionQueue: [], currentAction: null, stateRevision: 0, pendingFetches: 0,
      fetchScheduled: false, refreshRequested: false, loading: false,
      lastFetchFailed: false, consecutiveFetchFailures: 0,
      lastSuccessfulFetch: 'previous', sceneStatus: '', sceneStatusRoomId: '',
      actionError: '', opened: true, fetchDiagnostic: null, savedMusicOptions: {},
      sceneQueries: {}, scenePages: {}, scenePageSize: 6, prebuiltScenes: ['Relax', 'Read', 'Natural Light', 'Tv Mode']
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
  assert.equal(h.actionProc.command[5], 'put-all');
  assert.deepEqual(JSON.parse(h.actionProc.command[6]), ['uuid1', 'uuid2', 'uuid3']);
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
  assert.equal(h.actionProc.command[5], 'put-group');
  assert.equal(h.actionProc.command[6], 'bridge-service');
  assert.equal(h.root.orphanLights[0].on, false);
});

test('refresh waits for exit and stdout, commits all resources, keeps picker state', () => {
  const h = harness(); load(h);
  h.root.toggleColorPicker('1');
  h.root.refresh(); h.flush();
  assert.equal(h.stateProc.command[5], 'get-state');
  Object.assign(h.stateProc, {fetchOutput: snapshot(), fetchOutputFinished: true, fetchErrorFinished: true});
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
    Object.assign(h.stateProc, {fetchOutput: bad, fetchOutputFinished: true, fetchErrorFinished: true, fetchExited: true, fetchExitCode: 0});
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
  Object.assign(h.stateProc, {fetchOutput: snapshot(), fetchOutputFinished: true, fetchErrorFinished: true, fetchExited: true, fetchExitCode: 0});
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
  assert.equal(h.actionProc.command[5], 'write-theme-config');
  completeAction(h);
  assert.equal(h.actionProc.command[5], 'sync-room');
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
  h.actionProc.errorOutput = JSON.stringify({error: {code: 'auth', message: 'Bridge authentication failed'}});
  h.actionProc.actionOutputFinished = true;
  h.root.tryCompleteAction(); h.flush();
  assert.equal(h.root.actionError, 'Authentication failed. Re-pair the bridge to renew access.');
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
  Object.assign(h.stateProc, {fetchOutput: snapshot(), fetchOutputFinished: true, fetchErrorFinished: true, fetchExited: true, fetchExitCode: 0});
  h.root.tryCompleteFetch(h.stateProc);
  assert.equal(h.root.lightsById['2'].on, false);
  assert.equal(h.root.actionError, 'Hue command failed. Refresh and try again.');
});


test('timed-out commands release the queue and report a useful error', () => {
  const h = harness(); load(h);
  h.root.toggleLight('1', false);
  assert.deepEqual(Array.from(h.actionProc.command.slice(0, 3)), ['timeout', '--kill-after=2s', '30s']);
  h.root.toggleLight('2', true);
  completeAction(h, 124);
  assert.match(h.root.actionError, /timed out/);
  assert.equal(h.actionProc.command[6], 'uuid2');
  completeAction(h);
  assert.equal(h.root.currentAction, null);
  assert.equal(h.resyncTimer.count, 1);
});


test('failed helper starts release fetch and action bookkeeping without stream or exit signals', () => {
  const h = harness(); load(h);
  const oldLights = h.root.lightsById;
  h.root.refresh(); h.flush();
  h.stateProc.running = false;
  h.root.checkFetchStart();
  h.root.checkFetchStart();
  assert.equal(h.root.pendingFetches, 0);
  assert.equal(h.root.fetchDiagnostic.code, 'helper');
  assert.equal(h.root.lightsById, oldLights);
  assert.equal(h.root.consecutiveFetchFailures, 1);
  h.root.refresh(); h.flush();
  Object.assign(h.stateProc, {running: false, fetchOutput: snapshot(),
    fetchOutputFinished: true, fetchErrorFinished: true, fetchExited: true, fetchExitCode: 0});
  h.root.tryCompleteFetch(h.stateProc);
  h.root.checkFetchStart();
  assert.equal(h.root.fetchDiagnostic, null);

  h.root.applyScene('4', 'Relax');
  h.root.toggleLight('2', true);
  h.actionProc.running = false;
  h.root.checkActionStart();
  h.root.checkActionStart();
  assert.match(h.root.actionError, /helper could not start/);
  assert.equal(h.root.sceneStatus, 'Could not apply Relax');
  assert.equal(h.themeConfigFile.count, 1);
  h.flush();
  assert.equal(h.actionProc.command[5], 'put-light');
  completeAction(h);
  assert.equal(h.root.currentAction, null);
  assert.equal(h.resyncTimer.count, 1);
});

test('queue admission preserves the FIFO, permits tail replacement, and rejects optimistic updates', () => {
  const h = harness(); load(h);
  h.root.themeSync = {4: false};
  h.root.toggleLight('1', false);
  for (let i = 0; i < h.root.maxQueuedActions - 1; i++) h.root.toggleLight('1', true);
  const revision = h.root.stateRevision;
  h.root.toggleThemeSync('4');
  assert.equal(h.root.themeSync['4'], false);
  assert.equal(h.root.stateRevision, revision);
  h.root.setBrightness('1', 30);
  h.root.toggleLight('2', true);
  h.root.applyScene('4', 'Read');
  h.root.toggleFavorite('4');
  assert.equal(h.root.lightsById['2'].on, false);
  assert.equal(h.root.favoriteRooms['4'], undefined);
  assert.equal(h.root.sceneStatus, '');
  assert.match(h.root.actionError, /queue is full/);
  assert.equal(h.root.actionQueue.length, h.root.maxQueuedActions);
  assert.equal(h.root.setRoomBrightness('4', 55), false);
  assert.equal(h.root.setBrightness('2', 60), false);
  assert.equal(h.root.setColorTemperature('1', 250), false);
  assert.equal(h.root.setBrightness('1', 40), true);
  assert.equal(h.root.actionQueue.length, h.root.maxQueuedActions);
  assert.equal(h.root.lightsById['1'].bri, 40);
  for (let i = 0; i < h.root.maxQueuedActions; i++) {
    assert.equal(JSON.parse(h.actionProc.command[7]).on, i !== 0);
    completeAction(h);
  }
  assert.deepEqual(JSON.parse(h.actionProc.command[7]), {bri: 40});
  completeAction(h);
  h.root.toggleThemeSync('4');
  assert.equal(h.root.themeSync['4'], true);
  assert.equal(h.actionProc.command[5], 'write-theme-config');
  completeAction(h);
  assert.equal(h.actionProc.command[5], 'sync-room');
});

test('rejected music drafts preserve queued favorites and admitted music options', () => {
  const h = harness(); load(h);
  h.root.applyThemeSettings({musicOptions: {4: {pace: 1}}});
  h.root.toggleLight('1', false);
  h.root.toggleLight('1', true);
  h.root.toggleFavorite('4');
  h.root.setMusicOption('4', 'pace', 1.5);
  h.root.flushMusicOptions();
  while (h.root.actionQueue.length < h.root.maxQueuedActions) h.root.toggleLight('1', true);

  h.root.setMusicOption('4', 'pace', 2);
  h.root.setMusicOption('5', 'pace', 2);
  h.root.flushMusicOptions();
  assert.match(h.root.actionError, /queue is full/);
  assert.equal(h.root.musicOption('4', 'pace'), 1.5);
  assert.equal(h.root.musicOption('5', 'pace'), 1);
  assert.equal(h.themeConfigFile.count, 0);
  assert.equal(h.root.favoriteRooms['4'], true);

  completeAction(h);
  h.root.toggleFavorite('5');
  const command = h.root.actionQueue.at(-1).command;
  assert.equal(command[2], 'write-favorite-config');
  assert.deepEqual(JSON.parse(command[3]), {4: true, 5: true});
});

test('100 pending brightness changes launch only the first and latest helper', t => {
  const h = harness(); load(h);
  for (let i = 1; i <= 100; i++) h.root.setBrightness('1', i);
  assert.equal(h.root.actionQueue.length, 1);
  assert.equal(JSON.parse(h.actionProc.command[7]).bri, 1);
  completeAction(h);
  assert.equal(JSON.parse(h.actionProc.command[7]).bri, 100);
  completeAction(h);
  assert.equal(h.root.currentAction, null);
  assert.equal(h.root.lightsById['1'].bri, 100);
  t.diagnostic('Fixture burst: 100 brightness updates -> 2 helper launches (baseline: 100)');
});

test('toggles, scenes, targets and properties preserve queue ordering', () => {
  const h = harness(); load(h);
  h.root.setBrightness('1', 10);
  h.root.setBrightness('1', 20);
  h.root.toggleLight('1', false);
  h.root.setBrightness('1', 30);
  h.root.setBrightness('2', 40);
  h.root.lightsById['1'].ctMin = 153;
  h.root.lightsById['1'].ctMax = 500;
  h.root.setColorTemperature('1', 250);
  h.root.applyScene('4', 'Read');
  assert.equal(h.root.actionQueue.length, 6);
  const executed = [];
  while (h.root.currentAction) {
    executed.push(Array.from(h.root.currentAction.command.slice(2)));
    completeAction(h);
  }
  assert.deepEqual(executed.map(args => args[0]), [
    'put-light', 'put-light', 'put-light', 'put-light', 'put-light', 'put-light', 'apply-scene'
  ]);
  assert.deepEqual(executed.slice(0, 6).map(args => JSON.parse(args[2])), [
    {bri: 10}, {bri: 20}, {on: false}, {bri: 30}, {bri: 40}, {ct: 250}
  ]);
});

test('color writes pass v2 snapshot gamut while retaining v1 gamut compatibility', () => {
  const h = harness(); load(h);
  const gamut = {red: {x: 0.7, y: 0.3}, green: {x: 0.17, y: 0.7}, blue: {x: 0.15, y: 0.06}};
  h.root.lightsById['1'].gamut = gamut;
  h.root.setLightColor('1', 50000, 254);
  assert.deepEqual(JSON.parse(h.actionProc.command[8]), gamut);
  completeAction(h);
  h.root.config.apiVersion = 'v1';
  h.root.lightsById['1'].gamut = [[0.7, 0.3], [0.17, 0.7], [0.15, 0.06]];
  h.root.setLightColor('1', 1000, 200);
  assert.equal(h.actionProc.command.length, 9);
});

test('room scene browser includes saved scenes, preserves duplicate names, and scopes by room', () => {
  const h = harness(); load(h);
  const room = h.root.roomById('4');
  room.apiId = 'room-uuid';
  h.root.scenes = [
    {id: 'b', name: 'Sunset', groupApiId: room.apiId, active: false},
    {id: 'a', name: 'Sunset', groupApiId: room.apiId, active: true},
    {id: 'read', name: 'Read', groupApiId: room.apiId, active: false},
    {id: 'elsewhere', name: 'Other scene', groupApiId: 'other-room', active: false},
  ];
  const choices = h.root.roomSceneChoices(room);
  assert.equal(choices.length, 6);
  assert.equal(choices[0].id, 'a');
  assert.equal(choices.filter(choice => choice.name === 'Read').length, 1);
  assert.equal(choices.filter(choice => choice.name === 'Sunset').length, 2);
  assert.ok(!choices.some(choice => choice.id === 'elsewhere'));
  h.root.applySceneChoice('4', choices[0]);
  assert.equal(h.actionProc.command[5], 'recall-scene');
  assert.equal(h.actionProc.command[6], 'a');
  assert.equal(h.actionProc.command[7], room.controlId);
  completeAction(h);
  assert.equal(h.root.sceneStatus, 'Applied Sunset');
  h.root.applySceneChoice('4', choices.find(choice => choice.preset));
  assert.equal(h.actionProc.command[5], 'apply-scene');
});

test('scene pages stay bounded, filter by name, and clamp after a refresh', () => {
  const h = harness(); load(h);
  const room = h.root.roomById('4'); room.apiId = 'room';
  h.root.scenes = Array.from({length: 25}, (_, i) => ({id: 'id' + i, name: 'Saved ' + i, groupApiId: 'room', active: false}));
  assert.equal(h.root.roomSceneCount(room), 29);
  assert.equal(h.root.roomScenePage(room).length, 6);
  h.root.changeScenePage(room, 999);
  assert.equal(h.root.roomScenePage(room).length, 5);
  h.root.setSceneQuery('4', 'saved 24');
  assert.equal(h.root.roomScenePageIndex(room), 0);
  assert.equal(h.root.roomScenePage(room)[0].id, 'id24');
  h.root.setSceneQuery('4', 'no match');
  assert.equal(h.root.roomScenePage(room).length, 0);
  h.root.setSceneQuery('4', '');
  h.root.changeScenePage(room, 3);
  h.root.scenes = [];
  assert.equal(h.root.roomScenePageIndex(room), 0);
  assert.equal(h.root.roomScenePage(room).length, 4);
});

test('fetch waits for stderr and distinguishes diagnostics while preserving the last snapshot', () => {
  for (const code of ['auth', 'tls', 'timeout', 'response', 'connection']) {
    const h = harness(); load(h);
    const oldLights = h.root.lightsById;
    h.root.refresh(); h.flush();
    Object.assign(h.stateProc, {fetchOutput: '', fetchOutputFinished: true, fetchExited: true, fetchExitCode: 1});
    h.root.tryCompleteFetch(h.stateProc);
    assert.equal(h.root.pendingFetches, 1);
    Object.assign(h.stateProc, {fetchErrorFinished: true, fetchError: JSON.stringify({error: {code, message: 'secret raw response'}})});
    h.root.tryCompleteFetch(h.stateProc);
    assert.equal(h.root.fetchDiagnostic.code, code);
    assert.equal(h.root.lightsById, oldLights);
    assert.ok(!JSON.stringify(h.root.fetchDiagnostic).includes('secret'));
    h.root.refresh(); h.flush();
    Object.assign(h.stateProc, {fetchOutput: snapshot(), fetchOutputFinished: true, fetchErrorFinished: true, fetchExited: true, fetchExitCode: 0});
    h.root.tryCompleteFetch(h.stateProc);
    assert.equal(h.root.fetchDiagnostic, null);
  }
});

test('watchdog and malformed successful output produce distinct diagnostics', () => {
  const h = harness(); load(h);
  assert.equal(h.HueApi.connectionDiagnostic('', 124, false).code, 'timeout');
  assert.equal(h.HueApi.connectionDiagnostic('', 137, false).code, 'timeout');
  h.root.refresh(); h.flush();
  Object.assign(h.stateProc, {fetchOutput: '{}', fetchOutputFinished: true, fetchErrorFinished: true, fetchExited: true, fetchExitCode: 0});
  h.root.tryCompleteFetch(h.stateProc);
  assert.equal(h.root.fetchDiagnostic.code, 'response');
  h.root.applyScene('4', 'Relax');
  completeAction(h, 1, JSON.stringify({error: {code: 'tls'}}));
  assert.match(h.root.actionError, /TLS connection failed/);
});

test('oversized snapshots are rejected before publishing delegate models', () => {
  const h = harness(); load(h);
  const modelKeys = ['lightsById', 'rooms', 'scenes', 'allControlId', 'roomsWithLights', 'orphanLights'];
  const oldModels = modelKeys.map(key => h.root[key]);
  const entries = (count, row) => Object.fromEntries(Array.from({length: count}, (_, i) => [i + 1, row]));
  const light = {state: {on: true}};
  const room = {type: 'Room', lights: Array(256).fill('1'), state: {}};
  const raw = {lights: {}, groups: {}, scenes: {}, allControlId: 'replacement'};
  for (const changes of [
    {lights: entries(193, light)},
    {groups: entries(65, {...room, lights: []})},
    {scenes: entries(513, {})},
    {groups: {1: {...room, lights: Array(257).fill('1')}}},
    {groups: {1: room, 2: room, 3: {...room, lights: ['1']}}},
    {lights: {1: {...light, name: '🌈'.repeat(65)}}},
    {groups: {1: {...room, name: 'x'.repeat(129)}}},
    {scenes: {1: {name: 'x'.repeat(129)}}},
    {extra: 'x'.repeat(524288)}
  ]) {
    h.root.refresh(); h.flush();
    Object.assign(h.stateProc, {
      fetchOutput: JSON.stringify({...raw, ...changes}), fetchOutputFinished: true,
      fetchErrorFinished: true, fetchExited: true, fetchExitCode: 0
    });
    h.root.tryCompleteFetch(h.stateProc);
    assert.equal(h.root.fetchDiagnostic.code, 'response');
    modelKeys.forEach((key, i) => assert.equal(h.root[key], oldModels[i]));
  }
  assert.ok(h.HueApi.parseSnapshot(JSON.stringify({
    ...raw, lights: entries(192, {...light, name: '🌈'.repeat(64)}), groups: {1: room, 2: room}
  })));
  const json = JSON.stringify(raw);
  const atLimit = json + ' '.repeat(524288 - json.length - 1) + '\n';
  assert.ok(h.HueApi.parseSnapshot(atLimit));
  assert.equal(h.HueApi.parseSnapshot(atLimit + '\n'), null);
});

test('snapshot IDs and group references cannot address inherited properties', () => {
  const h = harness(); load(h);
  const oldRooms = h.root.roomsWithLights;
  const oldOrphans = h.root.orphanLights;
  for (const id of ['__proto__', 'constructor', ['1']]) {
    const data = JSON.parse(snapshot());
    if (id === '__proto__') {
      Object.defineProperty(data.lights, id, {value: data.lights['1'], enumerable: true});
    } else {
      data.groups['4'].lights.push(id);
    }
    h.root.refresh(); h.flush();
    Object.assign(h.stateProc, {
      fetchOutput: JSON.stringify(data), fetchOutputFinished: true,
      fetchErrorFinished: true, fetchExited: true, fetchExitCode: 0
    });
    h.root.tryCompleteFetch(h.stateProc);
    assert.equal(h.root.fetchDiagnostic.code, 'response');
    assert.equal(h.root.roomsWithLights, oldRooms);
    assert.equal(h.root.orphanLights, oldOrphans);
  }
});

test('party commands are serialized and light order persists through the settings queue', () => {
  const h = harness();
  h.root.partyRooms = {};
  h.root.partyOrder = {};
  const room = {id: 'room', lights: [
    {id: '1', name: 'Desk', hasColor: true},
    {id: 'white', name: 'White', hasColor: false},
    {id: '2', name: 'Shelf', hasColor: true},
    {id: '3', name: 'Door', hasColor: true}
  ]};
  h.root.toggleParty('room');
  assert.equal(h.actionProc.command[5], 'party');
  assert.deepEqual(Array.from(h.actionProc.command.slice(6, 8)), ['start', 'room']);
  h.root.movePartyLight(room, 2, -1);
  assert.deepEqual(Array.from(h.root.partyOrder.room), ['1', '3', '2']);
  assert.equal(h.root.actionQueue[0].command[2], 'write-party-order');
  completeAction(h);
  assert.equal(h.actionProc.command[5], 'write-party-order');
  assert.equal(h.actionProc.command[7], '["1","3","2"]');
  h.root.partyRooms = {room: true};
  h.root.toggleParty('room');
  completeAction(h);
  assert.equal(h.actionProc.command[6], 'stop');
});

test('party ordering skips removed bulbs and preserves positions for off bulbs', () => {
  const h = harness();
  h.root.partyOrder = {room: ['gone', '2']};
  const room = {id: 'room', lights: [
    {id: '1', hasColor: true, on: true}, {id: '2', hasColor: true, on: false},
    {id: 'white', hasColor: false}, {id: '3', hasColor: true, on: true}
  ]};
  assert.deepEqual(Array.from(h.root.partyLights(room), l => l.id), ['2', '1', '3']);
  h.root.movePartyLight(room, 0, -1);
  assert.equal(h.root.currentAction, null);
});


test('music sync uses the shared queue and preserves both room options', () => {
  const h = harness();
  h.root.musicRooms = {};
  h.root.musicOptions = {};
  h.root.toggleMusic('room');
  assert.equal(h.actionProc.command[6], 'music-start');
  h.root.setMusicOption('room', 'intensity', 0.8);
  h.root.setMusicOption('room', 'sensitivity', 1.5);
  h.root.setMusicOption('room', 'pace', 1.25);
  assert.equal(h.root.musicOption('room', 'intensity'), 0.8);
  assert.equal(h.root.musicOption('room', 'sensitivity'), 1.5);
  assert.equal(h.root.musicOption('other', 'intensity'), 0.5);
  assert.equal(h.root.musicOption('other', 'sensitivity'), 1.0);
  assert.equal(h.root.musicOption('other', 'pace'), 1.0);
  assert.equal(h.root.musicOption('other', 'timing'), 0);
  h.root.flushMusicOptions();
  completeAction(h);
  assert.equal(h.actionProc.command[5], 'write-music-options');
  assert.equal(h.actionProc.command[7], '{"intensity":0.8,"sensitivity":1.5,"pace":1.25}');
  h.root.musicRooms = {room: true};
  h.root.toggleMusic('room');
  completeAction(h);
  assert.equal(h.actionProc.command[6], 'music-stop');
});

test('config reloads retain queued music and party order edits until their writes complete', () => {
  const h = harness();
  h.root.musicOptions = {};
  h.root.pendingMusicOptions = {};
  h.root.flushedMusicOptions = {};
  h.root.partyOrder = {};
  h.root.pendingPartyOrder = {};
  h.root.setMusicOption('room', 'sensitivity', 1.5);
  h.root.flushMusicOptions();                            // write A queued/in flight
  h.root.setMusicOption('room', 'sensitivity', 1.75);    // newer edit still pending
  const room = {id: 'room', lights: [{id: '2', hasColor: true}, {id: '1', hasColor: true}]};
  h.root.movePartyLight(room, 1, -1);                    // order write queued
  assert.deepEqual(Array.from(h.root.partyOrder.room), ['1', '2']);
  // A reload triggered by another settings write must not drop queued edits.
  h.root.applyThemeSettings({musicOptions: {}, partyOrder: {}});
  assert.equal(h.root.musicOption('room', 'sensitivity'), 1.75);
  assert.deepEqual(Array.from(h.root.partyOrder.room), ['1', '2']);
  completeAction(h);                                     // music write A finishes
  h.root.applyThemeSettings({musicOptions: {room: {sensitivity: 9}}, partyOrder: {}});
  assert.equal(h.root.musicOption('room', 'sensitivity'), 1.75); // pending still wins
  assert.equal(h.root.flushedMusicOptions.room, undefined);      // flushed override released
  completeAction(h);                                     // order write B finishes
  assert.equal(h.root.pendingPartyOrder.room, undefined);
  h.root.applyThemeSettings({musicOptions: {}, partyOrder: {}});
  assert.deepEqual(h.root.partyOrder.room, undefined);   // nothing optimistic remains
});

test('dragging music sliders batches the latest values and bounds pending writes', () => {
  const h = harness();
  h.root.musicOptions = {};
  for (let i = 0; i <= 100; i++) h.root.setMusicOption('room', 'intensity', i / 100);
  assert.equal(h.musicOptionsTimer.count, 1);
  assert.equal(h.root.currentAction, null);
  h.root.flushMusicOptions();
  assert.equal(h.actionProc.command[7], '{"intensity":1}');
  for (let i = 0; i < 100; i++) {
    h.root.setMusicOption('room', 'sensitivity', 0.25 + i / 100);
    h.root.flushMusicOptions();
  }
  assert.equal(h.root.actionQueue.length, 1);
  assert.equal(h.root.musicOption('room', 'sensitivity'), 1.24);
  completeAction(h);
  assert.equal(h.actionProc.command[7], '{"intensity":1,"sensitivity":1.24}');
});

test('concert brightness controls default to 10/75 and cannot cross each other', () => {
  const h = harness();
  h.root.musicOptions = {};
  assert.equal(h.root.musicOption('room', 'idle'), 0.1);
  assert.equal(h.root.musicOption('room', 'peak'), 0.75);
  h.root.setMusicOption('room', 'idle', 0.9);
  assert.equal(h.root.musicOption('room', 'idle'), 0.75);
  h.root.setMusicOption('room', 'peak', 0.5);
  assert.equal(h.root.musicOption('room', 'peak'), 0.75);
  h.root.setMusicOption('room', 'idle', 0.1);
  h.root.setMusicOption('room', 'peak', 0.8);
  h.root.flushMusicOptions();
  assert.equal(h.actionProc.command[7], '{"idle":0.1,"peak":0.8}');
});
