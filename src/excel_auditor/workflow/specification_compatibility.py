"""Explicit compatibility migration, preserving IDs, titles and layout settings."""
from .field_profiles import FieldProfile, compile_profile
from .models import WorkflowConfig, content_hash

COMPATIBILITY_VERSION = 1
TEXT_SPECIFICATIONS = {'尺寸','照射面积'}


def text_specification_profile(profile):
    result=FieldProfile.model_validate(profile).model_copy(deep=True)
    changes=[]
    for category in result.local_categories:
        for member in ('attributes','specifications'):
            for field in getattr(category,member):
                if field.title in TEXT_SPECIFICATIONS and field.type!='string':
                    changes.append({'category':category.id,'field':field.id,'title':field.title,'before':field.type,'after':'string'})
                    field.type='string';field.min=None;field.max=None
    return result,changes


def upgraded_snapshot(task, published_snapshot=None):
    snapshot=dict(task['payload']['field_snapshot'])
    profile,changes=text_specification_profile(snapshot['profile'])
    previous={'version':snapshot['version'],'sha256':snapshot['sha256']}
    snapshot.update(profile=profile.model_dump(mode='json'),sha256=content_hash(profile.model_dump(mode='json')),
                    compatibility_version=COMPATIBILITY_VERSION,matching_scope='all_fields',upgraded_from=previous)
    if published_snapshot and snapshot['sha256']==published_snapshot['sha256']:
        snapshot['version']=published_snapshot['version']
    config=compile_profile(profile)
    old=WorkflowConfig.model_validate(task['payload']['config'])
    for option in ('preserve_extras','source_retention_version','source_header_layout','preserve_source_merges','exclude_source_sequence','maintenance_header_heights'):
        setattr(config,option,getattr(old,option))
    by_id={f.rule.name:f for f in old.fields}
    for field in config.fields:
        if field.rule.name in by_id:
            prior=by_id[field.rule.name]
            field.header_path=prior.header_path
            field.maintenance_width=prior.maintenance_width
    return config,snapshot,changes
