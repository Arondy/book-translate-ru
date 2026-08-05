"""Shared step -> phase mapping for progress state (load_state.py, save_state.py).

Single source of truth: steps 1-9 map to phases 1-3, step 10 = "all done"
(finish). Eliminates the 1-9 vs 1-10 divergence between load_state.py and
save_state.py.
"""

PHASE_FOR_STEP = {
    1: 1,
    2: 1,
    3: 1,  # Phase 1: prepare, convert, glossary
    4: 2,
    5: 2,
    6: 2,  # Phase 2: plan, translate, merge
    7: 3,
    8: 3,
    9: 3,  # Phase 3: qa, polish, build
    10: 3,  # 10 = "all done"
}

PHASE_FILES = {
    1: "references/phase-1-prepare.md",
    2: "references/phase-2-translate.md",
    3: "references/phase-3-finish.md",
}

PHASE_NAMES = {
    1: "Подготовка (steps 1-3)",
    2: "Перевод (steps 4-6)",
    3: "Финал (steps 7-9)",
}
