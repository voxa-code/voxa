from server.danger import classify


# --- triggering: recursive/forced deletion ---------------------------------

def test_rm_rf_triggers():
    assert classify("rm -rf /tmp/build") is not None


def test_rm_fr_reversed_flags_triggers():
    assert classify("sudo rm -fr ./node_modules") is not None


def test_rm_dash_r_triggers():
    assert classify("rm -r old_logs") is not None


def test_rmdir_with_path_triggers():
    assert classify("rmdir /Users/me/scratch") is not None


def test_delete_the_repo_triggers():
    assert classify("please delete the repo") is not None


def test_delete_everything_triggers():
    assert classify("delete everything in this project") is not None


def test_wipe_triggers():
    assert classify("wipe the disk before we start") is not None


# --- triggering: git history rewrites / force pushes ------------------------

def test_force_push_phrase_triggers():
    assert classify("force push to origin main") is not None


def test_push_dash_dash_force_triggers():
    assert classify("git push --force origin main") is not None


def test_push_dash_f_triggers():
    assert classify("git push -f") is not None


def test_reset_hard_triggers():
    assert classify("git reset --hard HEAD~3") is not None


def test_git_clean_fd_triggers():
    assert classify("run git clean -fd to tidy up") is not None


def test_rebase_with_force_triggers():
    assert classify("rebase onto main with force") is not None


def test_branch_capital_d_triggers():
    assert classify("git branch -D main") is not None


def test_delete_the_branch_triggers():
    assert classify("delete the branch we don't need") is not None


# --- triggering: database destruction ---------------------------------------

def test_drop_table_triggers():
    assert classify("drop table users") is not None


def test_drop_database_triggers():
    assert classify("please drop database prod_db") is not None


def test_truncate_triggers():
    assert classify("truncate the orders table") is not None


# --- triggering: production deployment --------------------------------------

def test_deploy_to_production_triggers():
    assert classify("deploy to production now") is not None


def test_deploy_to_prod_triggers():
    assert classify("deploy this to prod") is not None


def test_push_live_triggers():
    assert classify("push it live") is not None


def test_release_to_app_store_triggers():
    assert classify("release it to the app store") is not None


# --- triggering: credential/key deletion ------------------------------------

def test_delete_api_key_triggers():
    assert classify("delete the api key") is not None


def test_delete_credentials_triggers():
    assert classify("delete my credentials") is not None


def test_revoke_ssh_key_triggers():
    assert classify("revoke the ssh key") is not None


# --- triggering: disk operations ----------------------------------------------

def test_format_disk_triggers():
    assert classify("format the disk") is not None


def test_diskutil_erase_triggers():
    assert classify("diskutil eraseDisk JHFS+ Untitled disk2") is not None


def test_mkfs_triggers():
    assert classify("mkfs.ext4 /dev/sdb1") is not None


def test_dd_of_dev_triggers():
    assert classify("dd if=/dev/zero of=/dev/disk2 bs=1m") is not None


# --- triggering: killing all processes / shutdown -----------------------------

def test_killall_triggers():
    assert classify("killall node") is not None


def test_kill_9_wildcard_triggers():
    assert classify("kill -9 -1") is not None


def test_kill_all_phrase_triggers():
    assert classify("kill all my processes") is not None


def test_shutdown_triggers():
    assert classify("shutdown the machine") is not None


def test_reboot_triggers():
    assert classify("reboot now") is not None


# --- triggering: chmod -R 777 --------------------------------------------------

def test_chmod_777_triggers():
    assert classify("chmod -R 777 /") is not None


# --- non-triggering: ordinary, safe phrasings ----------------------------------

def test_remove_unused_import_does_not_trigger():
    assert classify("remove the unused import") is None


def test_delete_this_function_does_not_trigger():
    assert classify("delete this function") is None


def test_drop_me_a_summary_does_not_trigger():
    assert classify("drop me a summary of the changes") is None


def test_kill_the_dev_server_does_not_trigger():
    assert classify("kill the dev server") is None


def test_delete_this_file_does_not_trigger():
    assert classify("delete this file, it's unused") is None


def test_delete_this_line_does_not_trigger():
    assert classify("delete this line of code") is None


def test_normal_commit_message_does_not_trigger():
    assert classify("commit this with message 'fix bug'") is None


def test_run_the_tests_does_not_trigger():
    assert classify("run the tests and tell me what fails") is None


def test_add_a_new_feature_does_not_trigger():
    assert classify("add a login page to the app") is None


def test_rm_single_file_does_not_trigger():
    assert classify("rm old_notes.txt") is None


def test_git_status_does_not_trigger():
    assert classify("what's the git status") is None


def test_push_normal_does_not_trigger():
    assert classify("push my changes") is None


def test_empty_text_does_not_trigger():
    assert classify("") is None


def test_none_text_does_not_trigger():
    assert classify(None) is None


def test_refactor_request_does_not_trigger():
    assert classify("refactor this function to be cleaner") is None


def test_kill_process_by_name_specific_does_not_trigger():
    assert classify("kill the process using port 3000") is None
