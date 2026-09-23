/** Run after compiling providerPreferences.ts. No server/provider is contacted. */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createRequire } from 'node:module';
import path from 'node:path';
const require = createRequire(import.meta.url);
const compiled = process.env.ADVISOR_COMPILED_DIR;
if (!compiled) throw new Error('Set ADVISOR_COMPILED_DIR to the directory containing providerPreferences.js');
const {
  emptyProviderPreferences, toggleProvider, toggleEffort, updateProvider,
  selectTransport, activeChoiceIds, groupModels, effectiveOptions, effortLabel,
} = require(path.resolve(compiled, 'providerPreferences.js'));
const options = [
  {choice_id:'oa-low', provider:'openai', model_id:'example-a', display_name:'Example A', reasoning_effort:'low', available:true},
  {choice_id:'oa-high', provider:'openai', model_id:'example-a', display_name:'Example A', reasoning_effort:'high', available:true},
  {choice_id:'ob-max', provider:'openai', model_id:'example-b', display_name:'Example B', reasoning_effort:'max', available:true},
  {choice_id:'glm-high', provider:'glm', model_id:'example-g', display_name:'Example G', reasoning_effort:'high', available:true},
];
function prefs() {
  let value = {...emptyProviderPreferences(), enabled:true, advisor_choice_id:'oa-high'};
  value = toggleEffort(value, 'openai', 'oa-low', true);
  value = toggleEffort(value, 'glm', 'glm-high', true);
  return value;
}
test('new defaults use gateway-preferred, no automatic model enrollment', () => {
  const value = emptyProviderPreferences();
  assert.equal(value.providers.openai.transport_preference, 'omniroute_preferred');
  assert.deepEqual(value.providers.openai.selected_choice_ids, []);
});
test('provider off remembers selections and re-enables ONLY those selections', () => {
  const value = prefs(); const off = toggleProvider(value, 'openai', false);
  assert.deepEqual(off.providers.openai.selected_choice_ids, ['oa-low']);
  assert.deepEqual(activeChoiceIds(off), ['glm-high']);
  assert.deepEqual(toggleProvider(off, 'openai', true), value);
});
test('switch does not disable the independent advisor', () => {
  assert.equal(toggleProvider(prefs(), 'openai', false).advisor_choice_id, 'oa-high');
});
test('collapse changes presentation only', () => {
  const value = prefs(); const collapsed = updateProvider(value, 'openai', {collapsed:true});
  assert.deepEqual(activeChoiceIds(value), activeChoiceIds(collapsed));
});
test('same model can have multiple independently selected efforts', () => {
  let value = toggleEffort(prefs(), 'openai', 'oa-high', true);
  value = toggleEffort(value, 'openai', 'ob-max', true);
  value = toggleEffort(value, 'openai', 'oa-low', false);
  assert.deepEqual(value.providers.openai.selected_choice_ids, ['oa-high','ob-max']);
});
test('toggle never mutates the previous preferences', () => {
  const value = prefs(); const before = JSON.stringify(value);
  toggleEffort(value, 'openai', 'oa-high', true);
  toggleProvider(value, 'glm', false);
  assert.equal(JSON.stringify(value), before);
});
test('all models listed, not only checked models', () => {
  assert.deepEqual(groupModels(options, 'openai').map(m=>m.model_id), ['example-a','example-b']);
});
test('only advertised effort levels rendered', () => {
  assert.deepEqual(groupModels(options,'openai')[0].options.map(o=>o.reasoning_effort), ['low','high']);
});
test('grouping does not collapse the same model string across families', () => {
  const other = {...options[3], model_id:'example-a'};
  assert.equal(groupModels([...options.slice(0,3),other],'glm')[0].model_id,'example-a');
});
test('duplicate transport rows must be collapsed by trusted logical adapter, not given extra weight', () => {
  assert.throws(()=>groupModels([...options,{...options[0],choice_id:'second-route'}],'openai'));
});
test('routing switch preserves semantic active pool and advisor', () => {
  const value = prefs(); const direct = selectTransport(value,'openai','direct_only');
  assert.deepEqual(effectiveOptions(value,options),effectiveOptions(direct,options));
  assert.equal(direct.advisor_choice_id,value.advisor_choice_id);
});
test('unavailable route does not silently remove semantic choice', () => {
  const reduced = options.map(o=>({...o,available:false}));
  assert.equal(effectiveOptions(prefs(),reduced).length,2);
});
test('saved selections survive JSON roundtrip while provider paused', () => {
  const value=toggleProvider(prefs(),'openai',false);
  assert.deepEqual(JSON.parse(JSON.stringify(value)),value);
});
test('new catalogue entries do not auto-select', () => {
  const expanded=[...options,{...options[2],model_id:'another',choice_id:'new'}];
  assert.deepEqual(effectiveOptions(prefs(),expanded).map(o=>o.choice_id),['oa-low','glm-high']);
});
test('wrong-family selected ID is rejected', () => {
  assert.throws(()=>effectiveOptions(toggleEffort(prefs(),'openai','glm-high',true),options));
});
test('empty or unresolved pool fails explicitly', () => {
  assert.throws(()=>effectiveOptions({...emptyProviderPreferences(),enabled:true},options));
  assert.throws(()=>effectiveOptions({...prefs(),unresolved_legacy_ids:['lost']},options));
});
test('migration route review is cleared only by explicit provider choice', () => {
  const value={...prefs(),route_review_required:['openai','glm']};
  assert.deepEqual(toggleProvider(value,'openai',false).route_review_required,['openai','glm']);
  assert.deepEqual(selectTransport(value,'openai','direct_only').route_review_required,['glm']);
});
test('missing paused selection remains stored but does not block another group', () => {
  const value=toggleProvider(toggleEffort(prefs(),'openai','lost',true),'openai',false);
  assert.deepEqual(effectiveOptions(value,options).map(o=>o.choice_id),['glm-high']);
  assert.ok(value.providers.openai.selected_choice_ids.includes('lost'));
});
test('unknown advertised effort gets its actual label, not a fabricated level', () => {
  assert.equal(effortLabel('provider-native'),'provider-native');
  assert.equal(effortLabel('not_applicable'),'No effort setting');
});
