const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2] || path.join(__dirname,'../scripts/management_ui/app.js'),'utf8');
const start = source.indexOf('  const dialogueMasters=',source.indexOf('async function openProductionSettings('));
const end = source.indexOf('  const currentMappings=',start);
function ids(job) {
  const context = vm.createContext({job});
  return JSON.parse(JSON.stringify(vm.runInContext(source.slice(start,end)+'\neffectiveSpeakers',context)));
}
const voice = {type:'dialogue',speaker:'narration',performance_beats:[{speaker:'narration',speech_mode:'narration'}]};
assert.deepEqual(ids({characters:[{id:'customer'},{id:'sales'}],planned_masters:[voice]}),['customer','sales']);
assert.deepEqual(ids({characters:[],planned_masters:[voice]}),[]);
assert.deepEqual(ids({planned_masters:[{type:'dialogue',speaker:'customer_voice',performance_beats:[{speaker:'customer_voice',speech_mode:'off_screen'}]}]}),[]);
assert.deepEqual(ids({planned_masters:[{type:'dialogue',speaker:'sales'}]}),['sales']);
assert.deepEqual(ids({characters:[{id:'silent_actor'}],planned_masters:[voice]}),['silent_actor']);
assert.deepEqual(ids({planned_masters:[{type:'dialogue',performance_beats:[{speaker:'sales',speakers:['sales','customer'],speech_mode:'on_screen'}]}]}),['sales','customer']);
console.log('6 production setup character assertions passed');
