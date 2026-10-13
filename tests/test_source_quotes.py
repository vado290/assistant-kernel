from assistant_kernel.services.source_quotes import actual_source_quote


def test_formatting_binds_actual_source_instead_of_inventing_a_quote():
    source='仅改 report abc （SHA 123）。保留原目标；不重跑 requirements/design，不外发。'
    assert actual_source_quote('仅改report abc（SHA123）。',source)=='仅改 report abc （SHA 123）'
    assert actual_source_quote('保留原目标；不重跑 requirements/design。',source)=='保留原目标；不重跑 requirements/design'
    assert actual_source_quote('不外发。',source)=='不外发。'


def test_semantic_changes_and_omissions_stay_rejected():
    source='保留原目标；不重跑 requirements/design，不外发。'
    assert actual_source_quote('保留原目标；重跑 requirements/design。',source) is None
    assert actual_source_quote('保留原目标……不外发。',source) is None
    assert actual_source_quote('保留原目标。不重跑 requirements/design。',source) is None
    assert actual_source_quote('。 ',source) is None
