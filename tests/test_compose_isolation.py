"""紙上與正式 Compose 的隔離檢查。

這個檔案存在的原因（2026-10-08 實測）：在 ~/bot-paper 打 `docker compose up -d`
少了 -f，用到的是同一個 repo 裡的 docker-compose.yml，container_name 直接撞上
**實盤**的 pionex-grid。Docker 報 "name is already in use" 中止——那是保護。
當時 docker-compose.paper.yml 還根本沒有 grid 服務，紙上網格無從啟動。

所以這裡鎖三件事：兩份設定的服務要一樣齊、container_name 不能撞、image 不能撞。
撞到任何一個，紙上測試就有可能動到真錢的容器。

執行：python tests/test_compose_isolation.py  或  python -m pytest tests/ -v
"""
from __future__ import annotations

import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name: str) -> dict:
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return yaml.safe_load(fh)["services"]


LIVE = _load("docker-compose.yml")
PAPER = _load("docker-compose.paper.yml")


def test_paper_has_every_live_service():
    """紙上少一個服務 = 那個東西根本沒辦法做紙上測試。"""
    missing = set(LIVE) - set(PAPER)
    assert not missing, f"紙上缺少服務：{sorted(missing)}"


def test_container_names_never_collide():
    """撞名的後果是：紙上的指令會碰到實盤的容器。"""
    live = {v.get("container_name") for v in LIVE.values()}
    paper = {v.get("container_name") for v in PAPER.values()}
    assert not (live & paper), f"container_name 撞名：{sorted(live & paper)}"


def test_every_service_names_its_container():
    """沒寫 container_name 會由 Docker 自動命名，隔離就不是可驗證的事實。"""
    for label, svcs in (("正式", LIVE), ("紙上", PAPER)):
        for name, v in svcs.items():
            assert v.get("container_name"), f"{label} 的 {name} 沒有 container_name"


def test_paper_container_names_are_marked_paper():
    for name, v in PAPER.items():
        assert "paper" in v["container_name"], \
            f"紙上的 {name} 容器名 {v['container_name']} 看不出是紙上的"


def test_images_never_collide():
    """共用 image tag 的話，紙上 build 會覆蓋掉實盤正在用的映像。"""
    live = {v.get("image") for v in LIVE.values()}
    paper = {v.get("image") for v in PAPER.values()}
    assert not (live & paper), f"image 撞名：{sorted(live & paper)}"


def test_paper_webhook_uses_a_different_host_port():
    """兩邊都綁 8080 的話，後起的那個會啟動失敗。"""
    def host_ports(svc):
        return {str(p).split(":")[0] for p in svc.get("ports", [])}
    live = host_ports(LIVE["webhook"])
    paper = host_ports(PAPER["webhook"])
    assert not (live & paper), f"對外埠衝突：{sorted(live & paper)}"


def test_paper_compose_warns_about_missing_dash_f():
    """少打 -f 是這次踩到的坑，檔案開頭必須寫清楚。"""
    with open(os.path.join(ROOT, "docker-compose.paper.yml"), encoding="utf-8") as fh:
        head = "".join(fh.readlines()[:30])
    assert "-f docker-compose.paper.yml" in head, "沒提醒要帶 -f"
    assert "docker rm" in head, "沒警告不要刪掉撞名的實盤容器"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  ❌ {fn.__name__}: {exc}")
    print("\n全部通過" if not failed else f"\n{failed} 個測試失敗")
    sys.exit(1 if failed else 0)
