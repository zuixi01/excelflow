"""Export-only dynamic columns; maintenance fields and saved task snapshots stay intact."""
from collections import Counter

from .models import OutputColumn, WorkflowConfig


def final_output_config(config: WorkflowConfig, profile: dict) -> WorkflowConfig:
    result = config.model_copy(deep=True)
    definitions = {}
    for category in profile.get('local_categories', []):
        for kind, member in [('attribute', 'attributes'), ('specification', 'specifications')]:
            for field in category.get(member, []):
                if field.get('enabled', True):
                    definitions[f"local.{category['id']}.{kind}.{field['id']}"] = (kind, field['title'], category['title'])
    for field in config.fields:
        original = definitions.get(field.rule.name)
        label = field.header_path[-1] if field.header_path else field.rule.title.split('/')[-1]
        if field.output_kind == 'normal':
            definitions.pop(field.rule.name, None)
        elif field.output_kind in {'attribute', 'specification'}:
            definitions[field.rule.name] = (field.output_kind, label, original[2] if original else '任务字段')
        elif original:
            definitions[field.rule.name] = (original[0], label, original[2])
    for output in result.outputs:
        for sheet in output.sheets:
            # Use the task's output order and field IDs, not current global settings or title guessing.
            buckets = {kind: [column.field for column in sheet.columns
                              if column.field in definitions and definitions[column.field][0] == kind]
                       for kind in ('attribute', 'specification')}
            grouped = {}
            for kind, title in [('attribute', '商品属性'), ('specification', '商品规格')]:
                sources = buckets[kind]
                if not sources:
                    continue
                counts = Counter(definitions[field][1] for field in sources)
                labels = [f'{definitions[field][2]}·{definitions[field][1]}' if counts[definitions[field][1]] > 1
                          else definitions[field][1] for field in sources]
                grouped[kind] = OutputColumn(title=f'动态字段/{title}', operation='key_value',
                                             sources=sources, source_labels=labels, width=60)
            columns, emitted = [], set()
            for column in sheet.columns:
                definition = definitions.get(column.field)
                if definition is None:
                    columns.append(column)
                elif definition[0] not in emitted:
                    columns.append(grouped[definition[0]])
                    emitted.add(definition[0])
            sheet.columns = columns
    return WorkflowConfig.model_validate(result.model_dump(mode='json'))
