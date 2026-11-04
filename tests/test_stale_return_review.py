import pytest
from openpyxl import load_workbook

from excel_auditor.workflow.models import EditRequest, WorkflowError
from test_flexible_maintenance_return import runtime, supplied, preview


def saved_return(runtime):
    _, service, task = runtime
    book = load_workbook(service.maintenance_file(task['id'], 'local'))
    book['维护表']['C4'] = '22'
    content = supplied(book)
    inspected = service.inspect_return(task['id'], 'local', content)
    reviewed = preview(runtime, inspected)
    saved = service.apply_return(task['id'], 'local', 'operator', reviewed['id'], True, confirm_structure=True)
    return book, content, reviewed, saved


def test_exact_repeat_is_success_without_mutation_or_export_including_legacy(runtime):
    store, service, task = runtime
    book, content, reviewed, saved = saved_return(runtime)
    # Backward compatibility with already-saved returns, including the user's file.
    with store.engine.begin() as con:
        old = store.get(con, reviewed['id'], 'local', 'return')
        old['payload'].pop('file_sha256', None)
        old['payload'].pop('applied_revision', None)
        store.save(con, old)
    result = service.inspect_return(task['id'], 'local', content)
    assert result['status'] == 'already_applied'
    assert result['payload'] == {'applied_revision': 2, 'current_revision': 2}
    assert service.get(task['id'], 'local')['revision'] == saved['revision']
    assert service.get(task['id'], 'local')['exports'] == []
    # Later edits are never reverted when a previously applied file is repeated.
    service.edit(task['id'], 'local', 'operator', EditRequest(base_revision=2, changes={'row_0': {'price': '33'}}))
    result = service.inspect_return(task['id'], 'local', content)
    assert result['payload'] == {'applied_revision': 2, 'current_revision': 3}
    _, rows = service.snapshot(task['id'], 'local', 3, include_rows=True)
    assert next(r for r in rows if r['id'] == 'row_0')['values_json']['price'] == '33'
    book.close()


def test_changed_old_file_compares_current_values_and_requires_explicit_confirmation(runtime):
    _, service, task = runtime
    book, _, _, _ = saved_return(runtime)
    # Old R1 workbook with an additional price edit and a removed row.
    book['维护表']['C4'] = '44'
    book['维护表'].delete_rows(6)
    inspected = service.inspect_return(task['id'], 'local', supplied(book))
    assert inspected['payload']['stale_revision'] is True
    assert inspected['payload']['source_revision'] == 1
    assert inspected['payload']['base_revision'] == 2
    reviewed = preview(runtime, inspected)
    assert reviewed['payload']['counts'] == {'changed': 1, 'added': 0, 'missing': 1}
    assert reviewed['payload']['samples'][0]['before']['price'] == '22'
    assert reviewed['payload']['samples'][0]['after']['price'] == '44'
    with pytest.raises(WorkflowError) as failure:
        service.apply_return(task['id'], 'local', 'operator', reviewed['id'], True, confirm_structure=True)
    assert failure.value.code == 'RETURN_STALE_CONFIRMATION_REQUIRED'
    assert service.get(task['id'], 'local')['revision'] == 2
    saved = service.apply_return(task['id'], 'local', 'operator', reviewed['id'], True,
                                 confirm_structure=True, confirm_stale=True)
    assert saved['revision'] == 3
    assert saved['payload']['record_count'] == 2
    assert saved['exports'] == []
    book.close()


def test_concurrent_update_after_old_file_review_still_blocks_save(runtime):
    _, service, task = runtime
    book, _, _, _ = saved_return(runtime)
    book['维护表']['C4'] = '44'
    reviewed = preview(runtime, service.inspect_return(task['id'], 'local', supplied(book)))
    service.edit(task['id'], 'local', 'operator', EditRequest(base_revision=2, changes={'row_0': {'price': '55'}}))
    with pytest.raises(WorkflowError) as failure:
        service.apply_return(task['id'], 'local', 'operator', reviewed['id'], True,
                             confirm_structure=True, confirm_stale=True)
    assert failure.value.status == 409
    assert service.get(task['id'], 'local')['revision'] == 3
    book.close()


def test_newer_rows_missing_from_old_workbook_are_shown_before_any_deletion(runtime):
    _, service, task = runtime
    book, _, _, _ = saved_return(runtime)
    _, rows = service.snapshot(task['id'], 'local', 2, include_rows=True)
    added = dict(rows[0]['values_json'], sku='later-product')
    service.edit(task['id'], 'local', 'operator', EditRequest(base_revision=2, additions=[added]))
    book['维护表']['C4'] = '44'
    reviewed = preview(runtime, service.inspect_return(task['id'], 'local', supplied(book)))
    assert reviewed['payload']['counts']['missing'] == 1
    assert reviewed['payload']['missing_samples'][0]['values']['sku'] == 'later-product'
    with pytest.raises(WorkflowError, match='旧版本'):
        service.apply_return(task['id'], 'local', 'operator', reviewed['id'], True, confirm_structure=True)
    assert service.get(task['id'], 'local')['payload']['record_count'] == 4
    book.close()


@pytest.mark.parametrize('key,value,code', [('task_id', 'other_task', 'RETURN_TASK_MISMATCH'),
    ('template_hash', 'incorrect', 'RETURN_TEMPLATE_MISMATCH'), ('revision', 999, 'REVISION_CONFLICT')])
def test_wrong_task_template_and_future_revision_remain_blocked(runtime, key, value, code):
    _, service, task = runtime
    book = load_workbook(service.maintenance_file(task['id'], 'local'))
    for row in book['_workflow_meta']:
        if row[0].value == key:
            row[1].value = value
    with pytest.raises(WorkflowError) as failure:
        service.inspect_return(task['id'], 'local', supplied(book))
    assert failure.value.code == code
    assert service.get(task['id'], 'local')['revision'] == 1
    book.close()
