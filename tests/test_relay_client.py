from server.relay_client import _with_params


def test_with_params_appends_only_nonempty():
    assert (_with_params("ws://x/ws?token=t", account="a", voice="", lang="ar")
            == "ws://x/ws?token=t&account=a&lang=ar")


def test_with_params_first_param_uses_question_mark():
    assert _with_params("ws://x/ws", lang="ar") == "ws://x/ws?lang=ar"
