from dataclasses import dataclass

@dataclass
class ProtocolModel:
    token_functions: list
    value_flows: list
    accounting_relations: list
    oracle_signals: list
    fee_signals: list
    transitions: list

def build_protocol_model(contracts):
    from .tokens import classify_token_functions
    from .flows import build_value_flows
    from .accounting import infer_accounting
    from .oracles import detect_oracle_signals
    from .fees import detect_fee_signals
    from .state_machine import infer_transitions

    return ProtocolModel(
        classify_token_functions(contracts),
        build_value_flows(contracts),
        infer_accounting(contracts),
        detect_oracle_signals(contracts),
        detect_fee_signals(contracts),
        infer_transitions(contracts),
    )
