#!/usr/bin/env python3
"""Local board for playing full games against jqe in a browser.

  python3 engine/tools/play.py [--port 8765] [--engine engine/build/jqe] [--net engine/nets/it014.jqn]

then open http://localhost:8765 . Everything stays on this machine: the server listens on 127.0.0.1 only and talks
to one local engine process; it connects to nothing else.

Referee: the project's native rules library (legality, check, repetition/chase, 80-ply rule), as in match.py.
The deal is random and hidden from both players. The browser is sent only what a player at the board sees: reveals
are public, the identity of a face-down piece you capture is shown to you alone, and the hidden identities are
sent when the game is over. The engine receives its own observation (the tokens match.py sends) and nothing else.
Finished games are appended to engine/runs/play/games.jsonl.
"""
import argparse
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

# 打包成 exe 之后 __file__ 落在 PyInstaller 的内部目录里，资源（play.html / 引擎 / 权重 / 裁判库）
# 要按 exe 所在的目录找；源码运行时还是按仓库结构找。
FROZEN = bool(getattr(sys, "frozen", False))
ROOT = Path(__file__).resolve().parents[2]
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else ROOT
if not FROZEN:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("JIEQI_NATIVE_LIBRARY", str(ROOT / "build/release/lib/libjieqi_rules.so"))
if FROZEN:  # exe 旁边放一份裁判库就行
    for cand in (APP_DIR / "engine/jieqi_rules.dll", APP_DIR / "jieqi_rules.dll"):
        if cand.is_file():
            os.environ["JIEQI_NATIVE_LIBRARY"] = str(cand)
            break

from jieqi.native import GameStatus, NativeGame, NativeRules  # noqa: E402
from jieqi.setup import Color, Kind, Piece, Setup, square  # noqa: E402

START_FEN = "xxxxkxxxx/9/1x5x1/x1x1x1x1x/9/9/X1X1X1X1X/1X5X1/9/XXXXKXXXX w R2A2C2P5N2B2r2a2c2p5n2b2 0 1"
INVENTORY = {"R": 2, "N": 2, "B": 2, "A": 2, "C": 2, "P": 5}
FEN_KINDS = {letter: Kind(letter) for letter in "ABNRCP"}
KIND_LETTER = {kind: letter for letter, kind in FEN_KINDS.items()}
FEN_LETTERS = "KABNRCP"
PAGE = APP_DIR / "play.html" if FROZEN else Path(__file__).with_name("play.html")
RECORDS = (APP_DIR / "runs/games.jsonl") if FROZEN else (ROOT / "engine/runs/play/games.jsonl")

CV = threading.Condition()  # guards every Game and the version counter; state requests wait on it
VERSION = 1                 # bumped on every visible change


def bump():
    global VERSION
    VERSION += 1
    CV.notify_all()


def parse_start_fen(fen):
    """5-field jieqi FEN -> (cells, side, pool).

    cells: 90 entries, None or (Color, letter, hidden); pool: {"red": {letter: n}, "black": {...}}.
    """
    parts = fen.split()
    if len(parts) != 5:
        raise ValueError("初始局面要写 5 段：棋盘 走棋方 身份池 空步 回合")
    ranks = parts[0].split("/")
    if len(ranks) != 10:
        raise ValueError("棋盘要正好 10 行")
    rows = []
    for index, rank in enumerate(ranks, start=1):
        width = 0
        row = []
        for ch in rank:
            if ch.isdigit():
                n = int(ch)
                if not 1 <= n <= 9:
                    raise ValueError(f"棋盘第 {index} 行有不合法的数字 {ch}")
                row.extend([None] * n)
                width += n
            elif ch in "Xx":
                row.append((Color.RED if ch == "X" else Color.BLACK, "", True))
                width += 1
            elif ch.upper() in FEN_LETTERS:
                color = Color.RED if ch.isupper() else Color.BLACK
                row.append((color, ch.upper(), False))
                width += 1
            else:
                raise ValueError(f"棋盘上不认识的符号 “{ch}”")
        if width != 9:
            raise ValueError(f"棋盘第 {index} 行不是 9 格")
        rows.append(row)
    # FEN 第一行是 rank 9，而内部按 rank 0（红方底线）打头，所以整排翻过来
    cells = [cell for row in reversed(rows) for cell in row]
    if len(cells) != 90:
        raise ValueError("棋盘要正好 90 格")
    turn = parts[1].lower()
    if turn not in ("w", "r", "b"):
        raise ValueError("走棋方要写 w（红先）或 b（黑先）")
    pool = {"red": {}, "black": {}}
    if parts[2] not in ("-", ""):
        for token in re.findall(r"[A-Za-z]\d+", parts[2]):
            letter, num = token[0], int(token[1:])
            if letter.upper() not in FEN_LETTERS or letter.upper() == "K":
                raise ValueError(f"身份池里不认识的兵种 “{letter}”")
            if num <= 0:
                continue
            side_name = "red" if letter.isupper() else "black"
            pool[side_name][letter.upper()] = pool[side_name].get(letter.upper(), 0) + num
    for side_name, color, label in (("red", Color.RED, "红"), ("black", Color.BLACK, "黑")):
        hidden = sum(1 for c in cells if c is not None and c[2] and c[0] is color)
        total = sum(pool[side_name].values())
        if total < hidden:
            raise ValueError(f"{label}方身份池写了 {total} 颗，棋盘上却有 {hidden} 颗暗子")
        # 池子可以比暗子多：多出来的那几颗是「摆子时删掉的、身份没定的」，发牌时随机挑 hidden 颗，
        # 剩下的就永远不出场（truth_from 只发 hidden 颗）。这样删掉一颗暗子不会锁死你能翻出什么。
        kings = sum(1 for c in cells if c is not None and not c[2] and c[0] is color and c[1] == "K")
        if kings != 1:
            raise ValueError(f"{label}方要正好 1 个{'帅' if color is Color.RED else '将'}，现在是 {kings} 个")
    return cells, Color.BLACK if turn == "b" else Color.RED, pool


def truth_from(cells, pool):
    """Deal the hidden identities out of each side's pool, at random."""
    rng = secrets.SystemRandom()
    bags = {}
    for side_name, color in (("red", Color.RED), ("black", Color.BLACK)):
        bag = [FEN_KINDS[k] for k, n in pool[side_name].items() for _ in range(n)]
        rng.shuffle(bag)
        bags[color] = bag
    truth = []
    for cell in cells:
        if cell is None:
            truth.append(None)
            continue
        color, letter, hidden = cell
        truth.append(Piece(color, bags[color].pop(), True) if hidden
                     else Piece(color, Kind("K") if letter == "K" else FEN_KINDS[letter], False))
    return truth


def public_of(truth):
    return "".join("." if p is None else (("X" if p.color is Color.RED else "x") if p.hidden else p.code())
                   for p in truth)


def fen_board(flat):
    """90 cells with '.' for empty -> the board field of a FEN (rows joined by '/', runs compressed)."""
    ranks = []
    for r in range(9, -1, -1):        # FEN 从 rank 9 写起
        out, run = [], 0
        for ch in flat[r * 9:r * 9 + 9]:
            if ch == ".":
                run += 1
                continue
            if run:
                out.append(str(run))
                run = 0
            out.append(ch)
        if run:
            out.append(str(run))
        ranks.append("".join(out))
    return "/".join(ranks)


def pool_string(pool):
    """R2A2C2P5N2B2r2a2c2p5n2b2 — uppercase red, lowercase black, in the engine's R A C P N B order."""
    return "".join((letter.lower() if side == "black" else letter) + str(pool[side][letter])
                   for side in ("red", "black") for letter in "RACPNB" if pool[side].get(letter))


class EngineError(RuntimeError):
    pass


class Engine:
    """One UCI engine process. Used by one thread at a time (the game's engine-turn thread)."""

    def __init__(self, path, options):
        self.proc = subprocess.Popen([str(path)], cwd=str(Path(path).parent), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        self.lines = queue.Queue()
        self.pondering = False
        self.start_fen = START_FEN  # the custom starting position, when the game has one
        self.busy = threading.Lock()  # one conversation with the engine at a time
        threading.Thread(target=self._reader, daemon=True).start()
        self.send("uci")
        self.wait_for("uciok", 10)
        for k, v in options.items():
            self.send(f"setoption name {k} value {v}")
        self.send("isready")
        self.wait_for("readyok", 120)  # a large hash table takes a moment
        self.send("ucinewgame")
        self.send("isready")
        self.wait_for("readyok", 120)

    def _reader(self):
        for line in self.proc.stdout:
            self.lines.put(line.rstrip("\n"))
        self.lines.put(None)

    def send(self, text):
        try:
            self.proc.stdin.write(text + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as e:
            raise EngineError(f"写入引擎失败：{e}")

    def wait_for(self, prefix, timeout, on_line=None):
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise EngineError(f"{timeout:.0f} 秒内没等到 {prefix}")
            try:
                line = self.lines.get(timeout=left)
            except queue.Empty:
                raise EngineError(f"{timeout:.0f} 秒内没等到 {prefix}")
            if line is None:
                raise EngineError("引擎进程退出了")
            if line.startswith(prefix):
                return line
            if on_line:
                on_line(line)

    def position(self, tokens):
        # tokens: (token, revealed). As in match.py, a lone reveal letter is padded with 'x'.
        toks = [t + "x" if rev and len(t) == 5 else t for t, rev in tokens]
        return "position fen " + self.start_fen + (" moves " + " ".join(toks) if toks else "")

    def go(self, tokens, go_args, timeout, on_line, fen=None):
        with self.busy:
            self._stop_ponder()
            self.send("position fen " + fen if fen else self.position(tokens))
            self.send("go " + go_args)
            parts = self.wait_for("bestmove", timeout, on_line).split()
            return parts[1] if len(parts) > 1 else "(none)"

    def ponder(self, tokens, fen=None):
        """Think on the position after our own move (the opponent to move) to fill the hash table."""
        with self.busy:
            self.send("position fen " + fen if fen else self.position(tokens))
            self.send("go infinite")
            self.pondering = True

    def _stop_ponder(self):
        if self.pondering:
            self.pondering = False
            self.send("stop")
            self.wait_for("bestmove", 15)

    def stop_ponder(self):
        with self.busy:
            self._stop_ponder()

    def close(self):
        try:
            self.send("stop")
            self.send("quit")
            self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()


def parse_info(line):
    """depth, seldepth, score, nodes, nps and pv of a UCI info line (None for other lines)."""
    t = line.split()
    if t[:2] != ["info", "depth"] or "score" not in t:
        return None
    out = {}
    for key in ("depth", "seldepth", "nodes", "nps", "hashfull"):
        if key in t:
            try:
                out[key] = int(t[t.index(key) + 1])
            except (ValueError, IndexError):
                pass
    i = t.index("score")
    try:
        out["score"] = {"kind": t[i + 1], "value": int(t[i + 2])}
    except (ValueError, IndexError):
        pass
    if "pv" in t:
        out["pv"] = t[t.index("pv") + 1:][:10]
    return out


class Game:
    """One human-against-engine game. All fields are guarded by CV."""

    def __init__(self, gid, cfg, engine_path, net, engine, restarter=None):
        self.id = gid
        self.cfg = cfg
        self.engine_path, self.net = str(engine_path), str(net)
        self.seed = cfg["seed"]
        self.human = Color(cfg["human"])
        if cfg.get("fen"):  # 自定义初始局面
            cells, side0, pool = parse_start_fen(cfg["fen"])
            self.truth = truth_from(cells, pool)
            self.pool0 = {"red": dict(pool["red"]), "black": dict(pool["black"])}
            public = public_of(self.truth)
            self.start_fen = (fen_board(public) + " " + ("w" if side0 is Color.RED else "b") + " "
                              + pool_string(pool) + " 0 1")
        else:
            self.truth = list(Setup.from_seed(self.seed).board)
            self.pool0 = {"red": dict(INVENTORY), "black": dict(INVENTORY)}
            public = public_of(self.truth)
            self.start_fen, side0 = START_FEN, Color.RED
        try:
            RULES.legal_moves(public, side0)  # rejects a position the referee cannot read
        except Exception as e:
            msg = str(e)
            if "dark piece is not on its original starting point" in msg:
                raise ValueError("这个局面不能用：暗子只能摆在开局位置上（车马炮士象兵各自的起始点），"
                                 "想摆到别处就先把它摆成明子") from None
            if "king outside own palace" in msg:
                raise ValueError("这个局面不能用：将和帅必须在自己的九宫里") from None
            raise ValueError(f"这个局面不能用：{msg}") from None
        self.native = NativeGame(RULES, public, side0)
        self.public0 = public     # the starting board, for replaying after an undo
        self.side0 = side0
        self.stack = []           # one entry per ply, so the human can take a move back
        self.gen = 0              # bumped by undo(): a search still running in _bot_turn is discarded
        self.pending = None       # a flip the human still has to name (the engine's move)
        self.cap_pending = None   # 被吃掉的暗子是什么：等人说（吃子方看得到身份）
        self.reveal_choice = None
        self.bot = self.human.opponent
        self.tokens = {Color.RED: [], Color.BLACK: []}  # each side's own observation of the game
        self.moves = []
        self.result = self.reason = None  # result: 1.0 red wins, 0.0 black wins, 0.5 draw
        self.tc = (cfg["base_ms"], cfg["inc_ms"]) if cfg["mode"] == "clock" else None
        self.clock = {Color.RED: float(cfg["base_ms"]), Color.BLACK: float(cfg["base_ms"])} if self.tc else None
        self.info, self.thinking, self.last_info = {}, False, 0.0
        self.started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.engine = engine
        self.restarter = restarter      # 悔棋时把正在搜的引擎进程整个换掉
        self.snap = self.native.snapshot()
        self.legal = ()
        self.turn_started = time.monotonic()
        self._after_move()

    # --- moves -----------------------------------------------------------------------------------------------
    def _apply(self, side, mv, used_ms, reveal_kind=None):
        o, d = square(mv[:2]), square(mv[2:4])
        mover, victim = self.truth[o], self.truth[d]
        if mover is None or mover.color is not side:
            raise ValueError("那不是你的棋子")
        if mover.hidden and reveal_kind is not None:  # 这一步翻开的是哪一颗，由人说
            if self.pool_now()[0][side.value].get(KIND_LETTER[reveal_kind], 0) <= 0:
                raise ValueError("这个兵种没有剩余了")
            mover = Piece(mover.color, reveal_kind, True)
            self.truth[o] = mover
        reveal = mover.kind if mover.hidden else None
        self.native.advance(mv[:4], reveal)  # raises on an illegal move
        self.stack.append({
            "truth": list(self.truth),
            "moves": list(self.moves),
            "tokens": {c: list(v) for c, v in self.tokens.items()},
            "clock": dict(self.clock) if self.clock else None,
        })
        own = opp = mv[:4]
        if reveal is not None:
            letter = reveal.value if side is Color.RED else reveal.value.lower()
            own += letter
            opp += letter
        if victim is not None and victim.hidden:
            own += victim.kind.value if victim.color is Color.RED else victim.kind.value.lower()  # capturer only
        self.tokens[side].append((own, reveal is not None))
        self.tokens[side.opponent].append((opp, reveal is not None))
        self.truth[d] = type(mover)(mover.color, mover.kind, False)
        self.truth[o] = None
        self.moves.append({"m": mv[:4], "by": side.value, "r": reveal.value if reveal else None,
                           "c": victim.kind.value if victim is not None else None,
                           "ch": bool(victim is not None and victim.hidden),
                           "ck": None, "t": round(used_ms)})
        if victim is not None and victim.hidden and self.result is None and self.cfg["manual"]:
            owner = victim.color.value
            pool = self.pool_now()[0][owner]
            self.cap_pending = {"idx": len(self.moves) - 1, "owner": owner, "mv": mv[:4],
                                "pool": {k: v for k, v in pool.items() if v > 0}}

    def engine_fen(self):
        """当前局面的揭棋 FEN（棋盘 + 走棋方 + 身份池 + 空步数 + 回合数）。

        直接用局面灌给引擎，而不是「start_fen + moves 历史」：引擎只要在某一手 token 上判
        失败（比如 reveal 不在它的 urn 里），就会把那一手之后的整段历史悄悄丢掉，之后两边
        盘面对不上。FEN 里带着身份池，等于把 urn 也一起给它，不会跑偏。
        """
        if self.result is not None:
            return None
        pool, _ = self.pool_now()
        side = "w" if self.snap.turn is Color.RED else "b"
        return (f"{fen_board(self.snap.board)} {side} {pool_string(pool)} "
                f"{self.snap.no_capture_plies} {1 + len(self.moves) // 2}")

    def _after_move(self):
        self.snap = self.native.snapshot()
        self.turn_started = time.monotonic()
        self.thinking = False
        if self.snap.status is not GameStatus.ONGOING:
            self._finish({GameStatus.DRAW: 0.5, GameStatus.RED_LOSES: 0.0, GameStatus.BLACK_LOSES: 1.0}[self.snap.status],
                         self.snap.status.name)
            return
        # 有「被吃掉的暗子」待指定时先锁住盘面，免得人家一边问一边又走子
        self.legal = (RULES.legal_moves(self.snap.board, self.snap.turn)
                      if self.snap.turn is self.human and self.cap_pending is None else ())
        bump()
        if self.snap.turn is self.bot:
            threading.Thread(target=self._bot_turn, daemon=True).start()

    def _finish(self, result, reason):
        if self.result is not None:
            return
        self.result, self.reason, self.thinking, self.legal = result, reason, False, ()
        eng = self.engine
        threading.Thread(target=eng.close, daemon=True).start()
        try:
            RECORDS.parent.mkdir(parents=True, exist_ok=True)
            with RECORDS.open("a") as f:
                f.write(json.dumps({"started": self.started, "seed": str(self.seed), "human": self.human.value,
                                    "result": result, "reason": reason, "engine": self.engine_path, "net": self.net,
                                    "config": self.cfg, "moves": self.moves}) + "\n")
        except OSError:
            pass
        bump()

    def pool_now(self):
        """(每方还没公开的身份, 每方被默默吃掉的颗数)。

        这里的池子必须和**引擎自己的 urn 完全一致**，否则会出大问题：引擎在 apply_token 里
        重构局面，只要某一步的 reveal 字母不在它的 urn 里（`reveal not in pool`），它就会
        把这一步之后的整段着法历史**悄悄丢掉**，于是它的盘面和我们这边彻底对不上——它接着
        会走出「红方去掀黑方棋子」这种诡异着法，最后判定 ILLEGAL_ENGINE_MOVE。

        引擎的 urn 只在两种情况下减少：己方翻开的暗子、以及**被它自己吃掉**的暗子
        （吃子方看得到身份，见 position.cpp:831-841）。所以「人吃引擎的暗子」不减少引擎的
        urn，我们这边也不能减——这一条以前写反了。
        """
        pool = {name: dict(self.pool0[name]) for name in ("red", "black")}
        unseen_lost = {"red": 0, "black": 0}
        over = self.result is not None
        for m in self.moves:
            if m["r"]:
                pool[m["by"]][m["r"]] = max(0, pool[m["by"]].get(m["r"], 0) - 1)
            if m["ch"]:
                owner = "black" if m["by"] == "red" else "red"
                if m["by"] == self.bot.value:  # 引擎吃下的暗子，它的身份从引擎的 urn 里消失
                    pool[owner][m["c"]] = max(0, pool[owner].get(m["c"], 0) - 1)
                if m["by"] != self.human.value and not over:
                    unseen_lost[owner] += 1
        return pool, unseen_lost

    def _ask_reveal(self, mv):
        """Hold the engine's move until the human says what the piece it just flipped is (None = whatever it was)."""
        pool = self.pool_now()[0][self.bot.value]
        with CV:
            self.pending = {"mv": mv, "by": self.bot.value, "from": mv[:2], "to": mv[2:4],
                            "pool": {k: v for k, v in pool.items() if v > 0}}
            self.reveal_choice = None
            bump()
            CV.wait_for(lambda: self.pending is None, timeout=180)
            kind, self.reveal_choice = self.reveal_choice, None
            self.pending = None
        return kind

    def set_capture(self, letter):
        """人回答「被吃掉的暗子是什么」（CV 已持有）。空/`?` = 不知道，就继续当暗子。"""
        if not self.cap_pending:
            raise ValueError("现在没有需要指定的被吃暗子")
        letter = (letter or "").strip().upper()
        m = self.moves[self.cap_pending["idx"]]
        if letter in ("", "?"):
            m["ck"] = None
        else:
            if letter not in FEN_KINDS:
                raise ValueError(f"不认识这个兵种 “{letter}”")
            if self.cap_pending["pool"].get(letter, 0) <= 0:
                raise ValueError("这个兵种没有剩余了")
            # 你指定的就是真值：池子扣减（引擎那一侧）、面板、记谱都按它走。
            # 不指定时 m["c"] 才是开局随机发牌的那一颗。
            m["ck"] = letter
            m["c"] = letter
        self.cap_pending = None
        self.legal = (RULES.legal_moves(self.snap.board, self.snap.turn)
                      if self.snap.turn is self.human else ())
        CV.notify_all()
        bump()

    def set_reveal(self, letter):
        """The human's answer to /api/reveal (called with CV held)."""
        if not self.pending:
            raise ValueError("现在不用选身份")
        letter = (letter or "").strip().upper()
        if letter in ("", "?"):
            self.reveal_choice = None  # 让它照本来的身份翻
        else:
            if letter not in FEN_KINDS:
                raise ValueError(f"不认识这个兵种 “{letter}”")
            if self.pending["pool"].get(letter, 0) <= 0:
                raise ValueError("这个兵种没有剩余了")
            self.reveal_choice = FEN_KINDS[letter]
        self.pending = None
        bump()

    def human_move(self, mv, reveal_kind=None, auto=False):
        if self.result is not None or self.snap.turn is not self.human:
            raise ValueError("还没轮到你走")
        if mv not in self.legal:
            raise ValueError("这步不合法")
        mover = self.truth[square(mv[:2])]
        if mover is not None and mover.hidden and self.cfg["manual"] and reveal_kind is None and not auto:
            raise ValueError("这一步要翻开暗子，请先选它是哪一颗")
        used = (time.monotonic() - self.turn_started) * 1000
        if self.tc:
            self.clock[self.human] += self.tc[1] - used  # shown, not enforced: the human's flag never falls
        self._apply(self.human, mv, used, reveal_kind)
        self._after_move()

    def undo(self):
        """撤回上一回合：退到又轮到你走为止（一般是「你一步 + jqe 一步」两手）。"""
        if not self.stack:
            raise ValueError("还没走过棋，没得悔")
        self.gen += 1                     # 正在搜的那一步作废
        self.cap_pending = None           # 待指定的被吃暗子随这一步一起撤掉
        if self.pending is not None:      # 引擎正等你指定身份：直接取消，让它别再落子
            with CV:
                self.pending, self.reveal_choice = None, None
                self.cap_pending = None
                bump()
        if self.thinking:
            self._swap_engine()           # 正在搜：关掉进程，让那一步彻底作废
        else:
            try:
                self.engine.stop_ponder()
            except EngineError:
                pass
        popped = 0
        while self.stack and popped < 2:
            self._pop_ply()
            popped += 1
            if self.result is None and self.snap.turn is self.human:
                break
        self.info, self.thinking = {}, False
        bump()

    def _swap_engine(self):
        try:
            self.engine.close()
        except Exception:  # noqa: BLE001 - a dead engine is fine here
            pass
        if self.restarter is not None:
            try:
                self.engine = self.restarter()
                self.engine.start_fen = self.start_fen
            except (EngineError, OSError):
                pass  # the next move will surface it

    def _pop_ply(self):
        """Restore the state recorded before the last ply (and rebuild the referee from the move list)."""
        st = self.stack.pop()
        self.truth = list(st["truth"])
        self.moves = list(st["moves"])
        self.tokens = {c: list(v) for c, v in st["tokens"].items()}
        self.clock = dict(st["clock"]) if st["clock"] else None
        self.result = self.reason = None
        self.native.close()
        self.native = NativeGame(RULES, self.public0, self.side0)
        for m in self.moves:  # replay: the reveal letter is what the piece turned into
            self.native.advance(m["m"], Kind(m["r"]) if m["r"] else None)
        self.snap = self.native.snapshot()
        self.legal = RULES.legal_moves(self.snap.board, self.snap.turn) if self.snap.turn is self.human else ()
        self.turn_started = time.monotonic()

    def resign(self):
        if self.result is None:
            self._finish(0.0 if self.human is Color.RED else 1.0, "RESIGNATION")

    def abandon(self):
        """A new game replaces this one."""
        with CV:
            self.gen += 1            # 在飞的搜索 / 等待「翻成什么」的回答一起作废
            self.pending = None
            self.reveal_choice = None
            if self.result is None:
                self.result, self.reason, self.thinking = 0.5, "ABANDONED", False
            bump()
        threading.Thread(target=self.engine.close, daemon=True).start()

    def _on_line(self, line):
        info = parse_info(line)
        if info is None:
            return
        with CV:
            self.info = info
            if time.monotonic() - self.last_info > 0.3:
                self.last_info = time.monotonic()
                bump()

    def _bot_turn(self):
        with CV:
            if self.result is not None:
                return
            CV.wait_for(lambda: self.cap_pending is None or self.result is not None, timeout=180)
            if self.result is not None:
                return
            eng, tokens = self.engine, list(self.tokens[self.bot])
            if self.tc:
                go = (f"wtime {max(1, int(self.clock[Color.RED]))} btime {max(1, int(self.clock[Color.BLACK]))} "
                      f"winc {self.tc[1]} binc {self.tc[1]}")
                timeout = max(self.clock[self.bot], 0) / 1000 + 15
            else:
                go = f"movetime {self.cfg['movetime_ms']}"
                timeout = self.cfg["movetime_ms"] / 1000 * 2 + 15
            self.thinking, self.info = True, {}
            gen = self.gen
            bump()
        t0 = time.monotonic()
        human_wins = 1.0 if self.human is Color.RED else 0.0
        try:
            mv = eng.go(tokens, go, timeout, self._on_line, fen=self.engine_fen())
        except EngineError as e:
            with CV:
                self._finish(human_wins, f"ENGINE_FAILURE: {e}")
            return
        used = (time.monotonic() - t0) * 1000
        kind = None
        if len(mv) >= 4:  # 引擎翻开的暗子，也交给你说
            mover = self.truth[square(mv[:2])]
            if mover is not None and mover.hidden and self.cfg["manual"]:
                kind = self._ask_reveal(mv)
        with CV:
            if self.result is not None or gen != self.gen:
                return
            if self.tc:
                self.clock[self.bot] -= used
                if self.clock[self.bot] < 0:
                    self._finish(human_wins, "TIME_FORFEIT")
                    return
                self.clock[self.bot] += self.tc[1]
            try:
                self._apply(self.bot, mv, used, kind)
            except Exception as e:
                self._finish(human_wins, f"ILLEGAL_ENGINE_MOVE {mv}: {e}")
                return
            self._after_move()
            if self.result is None and self.cfg["ponder"]:
                try:
                    eng.ponder(self.tokens[self.bot], self.engine_fen())
                except EngineError:
                    pass  # surfaces at the engine's next move

    # --- what the human may see --------------------------------------------------------------------------------
    def state(self):
        over = self.result is not None
        snap = self.snap
        moves = []
        for m in self.moves:
            # 暗子是扣着被拿走的：只要没人指定过，就只发 "?"
            secret = bool(m["ch"]) and not m.get("ck")
            moves.append({"m": m["m"], "by": m["by"], "r": m["r"],
                          "c": "?" if secret else (m.get("ck") or m["c"]),
                          "ch": m["ch"], "t": m["t"]})
        # identities the human has not seen yet, per side
        pool, unseen_lost = self.pool_now()
        clocks = None
        if self.tc:
            c = dict(self.clock)
            if not over:
                c[snap.turn] -= (time.monotonic() - self.turn_started) * 1000
            clocks = {"red": int(c[Color.RED]), "black": int(c[Color.BLACK]), "running": None if over else snap.turn.value}
        out = {"v": VERSION, "id": self.id, "human": self.human.value, "turn": snap.turn.value, "board": snap.board,
               "moves": moves, "legal": list(self.legal), "over": over, "result": self.result, "reason": self.reason,
               "check": (not over) and RULES.in_check(snap.board, snap.turn), "clocks": clocks,
               "no_capture": snap.no_capture_plies, "pool": pool, "unseen_lost": unseen_lost,
               "engine": {"thinking": self.thinking, "name": Path(self.engine_path).name, **self.info},
               "pending": self.pending,
               "pending_cap": self.cap_pending,
               "config": {k: self.cfg[k] for k in ("mode", "base_ms", "inc_ms", "movetime_ms", "threads", "hash", "ponder", "manual")}}
        if over:
            out["truth"] = "".join("." if p is None else p.code(reveal=True) for p in self.truth)
        return out


class App:
    def __init__(self, engine_path, net, defaults):
        self.engine_path, self.net, self.defaults = engine_path, net, defaults
        self.game = None
        self.count = 0
        self.starting = threading.Lock()
        self.last_seen = time.monotonic()

    def config(self, d):
        def num(key, lo, hi, default, kind=float):
            try:
                v = kind(d.get(key, default))
            except (TypeError, ValueError):
                raise ValueError(f"参数 {key} 不合法")
            if not lo <= v <= hi:
                raise ValueError(f"参数 {key} 必须在 {lo} 到 {hi} 之间")
            return v
        human = d.get("human", "red")
        if human == "random":
            human = secrets.choice(["red", "black"])
        if human not in ("red", "black"):
            raise ValueError("human 只能是 red、black 或 random")
        mode = d.get("mode", "clock")
        if mode not in ("clock", "movetime"):
            raise ValueError("mode 只能是 clock 或 movetime")
        seed = d.get("seed")
        seed = secrets.randbits(63) if seed in (None, "") else int(seed)
        if not 0 <= seed < 2 ** 64:
            raise ValueError("seed 必须是 64 位无符号整数")
        fen = (d.get("fen") or "").strip()
        if fen:
            parse_start_fen(fen)  # fail here, before the engine is torn down
        return {"human": human, "mode": mode, "seed": seed, "fen": fen,
                "manual": bool(d.get("manual", True)),
                "base_ms": int(num("base_min", 0.5, 600, 15) * 60000), "inc_ms": int(num("inc_s", 0, 300, 10) * 1000),
                "movetime_ms": int(num("movetime_s", 0.1, 3600, 5) * 1000),
                "threads": num("threads", 1, 64, self.defaults["threads"], int),
                "hash": num("hash", 16, 16384, self.defaults["hash"], int), "ponder": bool(d.get("ponder", True))}

    def new_game(self, d):
        cfg = self.config(d)
        with self.starting:
            with CV:
                if self.game:
                    self.game.abandon()
                    bump()
            try:  # engine start-up (hash allocation) happens outside CV
                engine = Engine(self.engine_path, {"EvalFile": self.net, "Threads": cfg["threads"], "Hash": cfg["hash"]})
            except (EngineError, OSError) as e:
                raise ValueError(f"引擎启动失败：{e}")
            with CV:
                self.count += 1
                opts = {"EvalFile": self.net, "Threads": cfg["threads"], "Hash": cfg["hash"]}
                self.game = Game(self.count, cfg, self.engine_path, self.net, engine,
                                 lambda: Engine(self.engine_path, opts))
                engine.start_fen = self.game.start_fen  # 自定义局面要给引擎看到同一个起点
                bump()

    def state(self, since):
        with CV:
            self.last_seen = time.monotonic()
            CV.wait_for(lambda: VERSION != since, timeout=25)
            self.last_seen = time.monotonic()
            if self.game is None:
                return {"v": VERSION, "id": 0, "none": True, "defaults": self.defaults,
                        "engine": {"name": Path(self.engine_path).name}}
            return {**self.game.state(), "defaults": self.defaults}

    def watchdog(self):
        """Stop pondering when no browser has asked for the state for a while (the tab was closed)."""
        while True:
            time.sleep(15)
            with CV:
                game, idle = self.game, time.monotonic() - self.last_seen
            if game is not None and game.result is None and idle > 90:
                try:
                    game.engine.stop_ponder()
                except EngineError:
                    pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _local(self):
        # Only this machine's browser: the Host header must name the loopback address this server listens on.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host in ("localhost", "127.0.0.1"):
            return True
        self._json(403, {"error": "只允许本机访问"})
        return False

    def do_GET(self):
        if not self._local():
            return
        path, _, query = self.path.partition("?")
        if path == "/":
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/state":
            try:
                since = int(parse_qs(query).get("v", ["0"])[0])
            except ValueError:
                since = 0
            self._json(200, APP.state(since))
        else:
            self._json(404, {"error": "没找到"})

    def do_POST(self):
        if not self._local():
            return
        # A page from another site cannot set this header without a preflight, which is never answered.
        if self.headers.get("X-Jqe") != "1":
            self._json(403, {"error": "缺少 X-Jqe 请求头"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 65536:
                raise ValueError("请求太大")
            data = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/new":
                APP.new_game(data)
            elif self.path == "/api/move":
                with CV:
                    if APP.game is None or data.get("id") != APP.game.id:
                        raise ValueError("没有这局棋")
                    kind = str(data.get("reveal") or "").strip().upper()
                    auto = kind == "?"          # 页面上的「随便」，按它本来的身份翻
                    if auto:
                        kind = ""
                    if kind and kind not in FEN_KINDS:
                        raise ValueError(f"不认识这个兵种 “{kind}”")
                    APP.game.human_move(str(data.get("move", "")), FEN_KINDS[kind] if kind else None, auto)
            elif self.path == "/api/reveal":
                with CV:
                    if APP.game is None or data.get("id") != APP.game.id:
                        raise ValueError("没有这局棋")
                    APP.game.set_reveal(str(data.get("kind", "")))
            elif self.path == "/api/capture":
                with CV:
                    if APP.game is None or data.get("id") != APP.game.id:
                        raise ValueError("没有这局棋")
                    APP.game.set_capture(str(data.get("kind", "")))
            elif self.path == "/api/undo":
                with CV:
                    if APP.game is None or data.get("id") != APP.game.id:
                        raise ValueError("没有这局棋")
                    APP.game.undo()
            elif self.path == "/api/resign":
                with CV:
                    if APP.game is not None and data.get("id") == APP.game.id:
                        APP.game.resign()
            else:
                self._json(404, {"error": "没找到"})
                return
            self._json(200, {"ok": True})
        except Exception as e:
            self._json(400, {"error": str(e)})


def newest_engine():
    def key(p):  # jqe-v0.15g -> (0, 15, 'g')
        s = p.name[len("jqe-v"):]
        num = "".join(ch if ch.isdigit() or ch == "." else " " for ch in s).split()
        parts = [int(x) for x in num[0].split(".") if x] if num else []
        return (parts, s)
    if FROZEN:  # 打包版：exe 旁边的 engine\jqe.exe
        return APP_DIR / "engine/jqe.exe"
    cands = [p for p in (ROOT / "engine/bin").glob("jqe-v*") if "-" not in p.name[len("jqe-v"):]]
    return max(cands, key=key) if cands else ROOT / "engine/build/jqe"


class QuietServer(ThreadingHTTPServer):
    """浏览器刷新、关页面、切走时会把长轮询连接直接掐掉；默认实现会打一整段 traceback。"""

    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def main():
    global RULES, APP, RECORDS
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--engine", default=None, help="引擎可执行文件（默认取 engine/bin 下最新的 jqe-v*，否则 engine/build/jqe）")
    ap.add_argument("--net", default=str((APP_DIR if FROZEN else ROOT) / "engine/nets/it014.jqn"))
    ap.add_argument("--threads", type=int, default=4, help="新对局对话框里的默认线程数")
    ap.add_argument("--hash", type=int, default=1024, help="新对局对话框里的默认哈希大小（MB）")
    ap.add_argument("--records", default=str(RECORDS), help="对局结束后追加写入的文件")
    ap.add_argument("--no-open", action="store_true", help="打包版启动时不要自动打开浏览器")
    args = ap.parse_args()
    RECORDS = Path(args.records)
    engine = Path(args.engine) if args.engine else newest_engine()
    if not engine.is_absolute():
        engine = ROOT / engine
    net = Path(args.net)
    if not net.is_absolute():
        net = ROOT / net
    for p in (engine, net, PAGE):
        if not p.exists():
            sys.exit(f"missing {p}")
    # 退出时解释器会顺手 flush 引擎那几根管道，句柄已经关了就会抛 OSError，
    # 于是打出 "Exception ignored in: <_io.TextIOWrapper ...> OSError: [Errno 22]"。
    # 那只是收尾噪声，和棋局无关，这里只吞 OSError，别的照常报。
    sys.unraisablehook = lambda u: None if isinstance(u.exc_value, OSError) else sys.__unraisablehook__(u)

    RULES = NativeRules()
    APP = App(engine, net, {"threads": args.threads, "hash": args.hash})
    server = QuietServer(("127.0.0.1", args.port), Handler)
    threading.Thread(target=APP.watchdog, daemon=True).start()
    url = f"http://localhost:{args.port}/"
    print(f"揭棋对局界面：{url}   引擎 {engine.name}，权重 {net.name}   （Ctrl+C 停止）", flush=True)
    if FROZEN and not args.no_open:  # 双击 exe 时省得自己去点链接
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with CV:
            if APP.game:
                APP.game.abandon()


if __name__ == "__main__":
    main()
