"""Group dialogue refers to existing people, never a composite identity."""
from copy import deepcopy
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from factory import creative_ir, production_compiler, script_drafts
from factory.steps import s3_shots

IDS = ["wang_gong", "lin_jie", "zhang_zhuguan"]


def group_script():
    return {
        "title": "三类客户", "creative_thesis": "保留三人同框和共同台词",
        "characters": [{"id": key, "name": name} for key, name in zip(
            IDS, ["王工", "林姐", "张主管"])],
        "beats": [{
            "beat": "hook", "speaker": ",".join(IDS), "characters": IDS[:],
            "dialogue": "我们一起说这句话。", "duration_sec": 4,
            "visual_intent": "三人同框，同步开口。", "scene": "访谈棚",
            "source_ids": [], "broll_hint": "", "speech_mode": "on_screen",
        }],
    }


class GroupSpeakerReferencesTests(unittest.TestCase):
    def test_normalization_keeps_one_line_with_three_speakers(self):
        raw = group_script()
        normalized = script_drafts._normalize_variant(raw, allowed_source_ids=set())
        self.assertEqual(normalized["beats"][0].get("speakers"), IDS)
        self.assertEqual(normalized["beats"][0]["speaker"], IDS[0])
        self.assertEqual(normalized["beats"][0]["dialogue"], raw["beats"][0]["dialogue"])
        self.assertEqual(normalized["beats"][0]["visual_intent"], raw["beats"][0]["visual_intent"])
        self.assertEqual(len(normalized["beats"]), 1)

    def test_structured_plural_without_scalar_is_supported(self):
        raw = group_script()
        raw["beats"][0].pop("speaker")
        raw["beats"][0]["speakers"] = IDS[:]
        normalized = script_drafts._normalize_variant(raw, allowed_source_ids=set())
        self.assertEqual(normalized["beats"][0]["speakers"], IDS)

    def test_legacy_cached_group_does_not_create_fourth_character(self):
        raw = group_script()
        before = deepcopy(raw)
        board = script_drafts._storyboard(raw)
        self.assertEqual([c["id"] for c in board["characters"]], IDS)
        self.assertEqual(board["masters"][0]["speakers"], IDS)
        self.assertEqual(board["masters"][0]["performance_beats"][0]["speakers"], IDS)
        self.assertEqual(raw, before)

    def test_structured_group_survives_storyboard_and_compilation(self):
        raw = group_script()
        raw["beats"][0].update(speaker=IDS[0], speakers=IDS[:])
        board = script_drafts._storyboard(raw)
        ir = creative_ir.from_storyboard(board)
        self.assertEqual(list(ir.beats[0].speakers), IDS)
        result = production_compiler.compile_ir(ir, job_id="J-group", budget_cents=10000,
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5())
        self.assertTrue(result.compilable, result.blockers)
        self.assertEqual(set(result.spec.masters[0].speaker_ids), set(IDS))
        anchors = [n.node_id for n in result.spec.nodes if n.node_type == "character_anchor"]
        self.assertEqual(set(anchors), {f"character_anchor:{key}" for key in IDS})

    def test_ir_splits_delimited_ids_without_discarding_unknown_member(self):
        for separator in (",", "，", "、"):
            raw = group_script()
            raw["beats"][0]["speaker"] = separator.join([IDS[0], "unknown_member"])
            ir = creative_ir.from_storyboard(raw)
            self.assertEqual(ir.beats[0].speakers, (IDS[0], "unknown_member"))
            result = production_compiler.compile_ir(ir, job_id="J-unknown", budget_cents=10000,
                capabilities=production_compiler.ProviderCapabilities.seedance_2_5())
            self.assertTrue(any(issue.code == "speaker_unknown" for issue in result.blockers))

    def test_group_unknown_member_is_rejected_before_auto_character_creation(self):
        raw = group_script()
        raw["beats"][0]["speaker"] = IDS[0] + ",unknown_member"
        with self.assertRaisesRegex(ValueError, "unknown.*speaker"):
            script_drafts._storyboard(raw)

    def test_silent_cast_is_not_promoted_to_speakers(self):
        raw = group_script()
        raw["beats"][0]["speaker"] = IDS[0]
        board = script_drafts._storyboard(raw)
        self.assertEqual(board["masters"][0]["speakers"], [IDS[0]])
        self.assertEqual(set(board["masters"][0]["cast"]), set(IDS))

    def test_duplicate_group_ids_are_deduplicated(self):
        raw = group_script()
        raw["beats"][0]["speaker"] += ", " + IDS[0]
        normalized = script_drafts._normalize_variant(raw, allowed_source_ids=set())
        self.assertEqual(normalized["beats"][0]["speakers"], IDS)

    @unittest.skipUnless(shutil.which("node"), "Node is required for the real editor")
    def test_editor_roundtrip_replaces_stale_group_members(self):
        source = (ROOT / "scripts/management_ui/app.js").read_text(encoding="utf-8")
        helper = re.search(r"^function scriptSpeakerIds\(.*$", source, re.MULTILINE).group()
        editor = source[source.index("function editScript(draft){"):source.index("function beatPerformanceSummary(")]
        code = r"""
const vm = require('node:vm');
const payload = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const controls = {}, fields = {};
for (const name of ['speaker','dialogue','duration_sec','visual_intent','emotion','tone','pace','volume','facial_expression','gaze','gesture','body_action','broll_hint']) fields[name] = {value:String(payload.script.beats[0][name]||'')};
const section = {dataset:{editScriptBeat:'0'},classList:{toggle(){}}};
let html='', saved;
const context = {esc:String,openModal:(a,b,c)=>html=c,
  $:(selector,parent)=>{
    if(parent===section){if(selector==='[data-duration-input]')return fields.duration_sec;return fields[selector.match(/data-name="([^"]+)"/)[1]]}
    return controls[selector] ||= {value:'',checked:false};
  },$$:selector=>selector==='[data-edit-script-beat]'?[section]:[],
  api:async(url,options)=>{saved=JSON.parse(options.body);return {}},
  closeModal(){},closeDrawer(){},toast(){},loadPage:async()=>false};
vm.createContext(context);vm.runInContext(payload.code,context);
context.editScript({draft_id:'d',script:payload.script});
fields.speaker.value='lin_jie';
controls['#save-script-edit'].onclick().then(()=>process.stdout.write(JSON.stringify({html,saved})));
"""
        raw = group_script()
        raw["beats"][0].update(speaker=IDS[0], speakers=IDS[:])
        result = subprocess.run([shutil.which("node"), "-e", code], input=json.dumps(
            {"code": helper + "\n" + editor, "script": raw}),
            text=True, encoding="utf-8", capture_output=True, check=True)
        output = json.loads(result.stdout)
        self.assertIn('value="' + ','.join(IDS) + '"', output["html"])
        saved = output["saved"]["script"]["beats"][0]
        self.assertEqual(saved["speakers"], ["lin_jie"])
        self.assertEqual(saved["speaker"], "lin_jie")

    def test_director_prompt_preserves_all_shared_speakers_and_one_dialogue(self):
        beat = group_script()["beats"][0]
        beat.update(speaker=IDS[0], speakers=IDS[:], duration=4)
        lines = s3_shots._cinematic_director_time_beats(
            {"performance_beats": [beat]}, duration=4, editorial_duration=4)
        text = "".join(lines)
        for key in IDS:
            self.assertIn(key, text)
        self.assertEqual(text.count(beat["dialogue"]), 1)

    @unittest.skipUnless(shutil.which("node"), "Node is required for the real UI renderer")
    def test_studio_renderer_counts_three_and_names_group_members(self):
        source = (ROOT / "scripts/management_ui/app.js").read_text(encoding="utf-8")
        definitions = []
        for name in ("scriptSpeakerIds", "scriptSpeakerLabel", "studioScriptStage"):
            match = re.search(r"^function " + name + r"\(.*$", source, re.MULTILINE)
            if match:
                definitions.append(match.group())
        code = r"""
const vm = require('node:vm');
const payload = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const nodes = {};
const context = {state:{studioOutlineIndex:-1}, esc:String,
  $: selector => nodes[selector] ||= {}, studioWorkspace: (a,b,c) => c,
  bindStudioOutline: () => {}, openDraftDrawer: () => {}};
vm.createContext(context);
vm.runInContext(payload.code, context);
context.studioScriptStage({}, {drafts:[{draft_id:'d',status:'approved',script:payload.script}]});
process.stdout.write(nodes['#studio-stage-body'].innerHTML);
"""
        script = group_script()
        for key in IDS:
            script["beats"].append({**script["beats"][0], "speaker": key})
        result = subprocess.run([shutil.which("node"), "-e", code], input=json.dumps(
            {"code": "\n".join(definitions), "script": script}),
            text=True, encoding="utf-8", capture_output=True, check=True)
        self.assertIn("3 位角色", result.stdout)
        self.assertIn("王工、林姐、张主管", result.stdout)


if __name__ == "__main__":
    unittest.main()
