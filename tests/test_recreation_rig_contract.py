import copy
import importlib.util
from pathlib import Path
import unittest


def load():
    p=Path(__file__).resolve().parents[1]/'scripts/recreation/rig_contract.py'
    s=importlib.util.spec_from_file_location('rig_contract',p)
    m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m


def rig(domain='source_human'):
    return {'schema':'recreation-rig-contract-v1','rig_id':domain+'-A','role_id':'A','domain':domain,
            'unit':'model_relative' if domain=='source_human' else 'target_relative',
            'shape_provenance':'source_model_median' if domain=='source_human' else 'target_asset_authored',
            'parents':[-1,0,1],'bone_lengths':[1.,1.],
            'reference_pose':{'source_frame_index':0,'xyz':[[0,0,0],[0,-1,0],[0,-2,0]],'twist':'unobserved'},
            'body_scale':1.,'world_root_status':'unresolved','world_root_trajectory':None}


class RigContractTests(unittest.TestCase):
    def test_source_human_must_not_silently_use_target_asset_shape(self):
        value=rig();value['shape_provenance']='target_asset_authored'
        with self.assertRaises(ValueError):load().validate_rig(value)

    def test_unit_and_domain_cannot_be_interchanged(self):
        value=rig();value['unit']='target_relative'
        with self.assertRaises(ValueError):load().validate_rig(value)

    def test_unresolved_world_root_cannot_hide_an_inherited_trajectory(self):
        value=rig();value['world_root_trajectory']=[[3,4,5]]
        with self.assertRaises(ValueError):load().validate_rig(value)

    def test_reference_lengths_must_match_declared_fixed_bones(self):
        value=rig();value['reference_pose']['xyz'][2]=[0,-2.2,0]
        with self.assertRaises(ValueError):load().validate_rig(value)

    def test_invalid_topology_and_per_frame_scale_are_rejected(self):
        for field,data in [('parents',[-1,2,0]),('body_scale',[1.,1.1])]:
            value=rig();value[field]=data
            with self.assertRaises(ValueError):load().validate_rig(value)

    def test_valid_pair_is_separate_but_not_automatically_ready_to_retarget(self):
        a=rig();b=rig('target_animal');before=copy.deepcopy((a,b))
        r=load().validate_retarget_pair(a,b)
        self.assertTrue(r['contract_valid']);self.assertFalse(r['world_motion_ready'])
        self.assertFalse(r['retarget_ready']);self.assertEqual((a,b),before)

    def test_identity_mismatch_or_same_rig_id_does_not_map(self):
        for field,data in [('role_id','other'),('rig_id','source_human-A')]:
            a=rig();b=rig('target_animal');b[field]=data
            with self.assertRaises(ValueError):load().validate_retarget_pair(a,b)


if __name__=='__main__':unittest.main()
