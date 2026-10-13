import json

from assistant_kernel.services.directory_assessment import assess_directory,default_parameters,directory_disclosure


def test_directory_scan_reports_exact_metadata_without_content(tmp_path):
    root=tmp_path/'outside';root.mkdir();(root/'a.py').write_text('private-content-not-returned')
    (root/'nested').mkdir();(root/'nested/b.txt').write_text('abc')
    params=default_parameters();params['thresholds']['file_count']=1
    result=assess_directory(project_id='demo',root_key='outside',path=root,context='expand',source_refs=[],**params)
    assert result['observed_file_count']==2 and result['scan_complete']
    assert result['measurement_quality']['file_count']=='exact'
    assert result['disposition']=='pending'
    assert 'private-content-not-returned' not in json.dumps(result)
    assert '不会自动删除' in directory_disclosure([result])


def test_directory_budget_and_symlinks_preserve_lower_bound_not_false_exactness(tmp_path):
    root=tmp_path/'outside';root.mkdir()
    for i in range(4):(root/str(i)).write_text('abc')
    params=default_parameters();params['scan_limits']['max_entries']=2
    result=assess_directory(project_id='demo',root_key='outside',path=root,context='create',source_refs=[],**params)
    assert result['observed_file_count']==2 and not result['scan_complete']
    assert result['measurement_quality']=={'file_count':'lower_bound','total_bytes':'lower_bound'}
    linkroot=tmp_path/'links';linkroot.mkdir();(linkroot/'other').symlink_to(root,target_is_directory=True)
    result=assess_directory(project_id='demo',root_key='links',path=linkroot,context='expand',source_refs=[],**default_parameters())
    assert result['observed_file_count']==0 and not result['scan_complete']
    assert result['disposition']=='pending'
