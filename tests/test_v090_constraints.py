from crystal.research.constraint import derive_constraints

def test_constraints_are_conservative():
    class D:
        sequence=["V.a","V.b"]
        delta={"totalAssets":"+ARG","totalSupply":"0"}
    x=derive_constraints(["V.a","V.b"],[D()])
    assert x.expressions
    assert x.parameter_ranges["V.a"] == (0,2**16-1)
