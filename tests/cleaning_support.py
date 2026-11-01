"""测试中显式执行从适配阶段迁入 YAML 的硬件清洗。"""

from types import SimpleNamespace

from adaptation.cleaning_rules import (
    CleaningPoint,
    CleaningRulesHandler,
    ConfiguredCleaningRule,
)
from adaptation.models import Vendor
from impl.common.domain.policies import SimulationAdaptationPolicy


def clean_hardware(document, targets, policy=None):
    handler = CleaningRulesHandler(CleaningPoint.POST_REWRITE)
    handler._rules = [
        rule
        for rule in handler._rules
        if isinstance(rule, ConfiguredCleaningRule)
        and rule.category == "physical-interface-knob"
    ]
    device = SimpleNamespace(
        document=document,
        device=SimpleNamespace(vendor=Vendor(document.vendor)),
        profile=SimpleNamespace(
            simulation_adaptation=policy or SimulationAdaptationPolicy()
        ),
        mappings=[
            SimpleNamespace(role="NNI", action="rename", target_interface=name)
            for name in targets
        ],
    )
    context = SimpleNamespace(devices={"R1": device}, events=[])
    context.add_event = lambda kind, message, **details: context.events.append(
        dict(kind=kind, **details)
    )
    handler.process(context)
    return context.events
