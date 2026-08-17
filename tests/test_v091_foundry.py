
from crystal.research.foundry import detect_foundry, execute_hypothesis

def test_foundry_detection_is_nonfatal():
    c=detect_foundry()
    assert isinstance(c.forge,bool)
    assert isinstance(c.anvil,bool)

def test_foundry_refuses_unknown_abi():
    class C:
        name="V"
        functions=[]
    r=execute_hypothesis("/tmp/no-project",[C()],["V.foo"])
    assert r.status in {"UNAVAILABLE","UNSUPPORTED"}
    assert not r.reproducible
