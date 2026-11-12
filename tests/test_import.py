import pywmswebcontrolpro
from pywmswebcontrolpro.webcontrol import WebControlPro


def test_package_exports_web_control() -> None:
    assert pywmswebcontrolpro.__name__ == "pywmswebcontrolpro"
    assert WebControlPro.__name__ == "WebControlPro"
