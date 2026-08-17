
def test_protocol_modules_import():
    from crystal.protocol.model import build_protocol_model
    from crystal.protocol.invariants import derive_protocol_invariants
    assert callable(build_protocol_model)
    assert callable(derive_protocol_invariants)
