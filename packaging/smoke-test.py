# -*- coding: utf-8 -*-
"""打包后的实跑冒烟：**双击真的能起来吗？**

静态自查（check-export.py）只能证明"包里没混进个人数据"，
证明不了"它跑得起来" —— 打包最常见的翻车就是"打得出来、双击就崩"
（缺 hiddenimport、缺数据文件、排错了模块）。

所以这一步真启动它、等它把本地服务起起来、调一次接口，再关掉。

用法：
    python packaging/smoke-test.py            # 用 dist/AnonSight.exe
    python packaging/smoke-test.py 别的.exe
"""
import os
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(HERE, 'dist', 'AnonSight', 'AnonSight.exe')
sys.stdout.reconfigure(encoding='utf-8')

PORTS = [8777] + list(range(8800, 8820))
CRLF = bytes([13, 10])          # 用数字拼，躲开各种转义坑


def probe(port):
    """端口上已经有我们的服务了吗？

    ⚠ **用裸 socket，别用 urllib** —— 第一版写的 `urlopen(..., timeout=1.0)`
    配合"挨个试 20 个端口"，光探测就要 20 秒，于是把 **2.3 秒**的启动量成了 **21.2 秒**，
    还让我以为"文件夹模式没优化到"。（主人当场看出来：他手动双击是秒开的。）
    现在：连接超时 0.05 秒、只探 5 个端口、循环间隔 0.1 秒。
    """
    req = b'GET /api/version HTTP/1.0' + CRLF + b'Host: x' + CRLF + CRLF
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.05) as s:
            s.sendall(req)
            s.settimeout(0.3)
            return s.recv(2000).decode('utf-8', 'replace')
    except Exception:
        return None


def main():
    if not os.path.exists(EXE):
        print('✗ 找不到 %s' % EXE)
        return 1
    print('启动 %s …' % os.path.basename(EXE))
    print('（会弹出一个窗口，验证完自动关掉）\n')

    # 先记一下哪些端口本来就被占着，免得把"别的进程"当成它起来了
    occupied = set()
    for p in PORTS:
        with socket.socket() as s:
            try:
                s.bind(('127.0.0.1', p))
            except OSError:
                occupied.add(p)
    if occupied:
        print('  （这些端口原本就被占用，会跳过：%s）' % sorted(occupied))

    proc = subprocess.Popen([EXE], cwd=os.path.dirname(EXE))
    found, body = None, None
    t0 = time.time()
    try:
        # 只探它可能用的那几个端口（启动器就是从 8777 / 8800 起往后找的）
        watch = [p for p in ([8777] + list(range(8800, 8805))) if p not in occupied]
        while time.time() - t0 < 45:
            if proc.poll() is not None:
                print('✗ 进程提前退出了（退出码 %s）—— 大概率是缺模块或缺数据文件' % proc.returncode)
                return 1
            for p in watch:
                b = probe(p)
                if b:
                    found, body = p, b
                    break
            if found:
                break
            time.sleep(0.1)          # 高频探：本地连接失败是**立刻**返回的，不用等整秒

        if not found:
            print('✗ 等了 45 秒，没等到它的本地服务起来')
            return 1

        print('✓ 起来了：端口 %d，用时 %.1f 秒' % (found, time.time() - t0))
        print('  /api/version → %s' % body.strip()[:160])

        # 再看一眼"用户第一次打开会看到什么"：papers 该是空的
        d = os.path.join(os.path.dirname(EXE), 'papers')
        if os.path.isdir(d):
            items = os.listdir(d)
            if items:
                print('  ⚠ exe 旁边的 papers/ 里已经有 %d 项 —— 如果这是要在别人机器上跑的包，'
                      '说明有数据混进来了：%s' % (len(items), items[:4]))
            else:
                print('  ✓ exe 旁边的 papers/ 是空的（用户从零开始，符合预期）')
        else:
            print('  （还没建 papers/ —— 首次拖论文时会自动建）')

        print('\n✓ 冒烟通过：这个 exe 能起来。')
        print('  剩最后一步人工确认：**把 papers/ 清空**，然后自己双击一次，')
        print('  看设置里显示的是不是「尚未配置」——那是别人第一次打开的样子。')
        return 0
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        time.sleep(0.5)
        if proc.poll() is not None:
            print('\n（已关掉窗口）')


if __name__ == '__main__':
    sys.exit(main())
