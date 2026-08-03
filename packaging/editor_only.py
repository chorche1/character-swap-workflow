"""Gör en STAGAD kopia av appen till en ren Editor-app.

Körs BARA av scripts/build-friend-package.sh, mot en utpackad kopia —
aldrig mot repot självt. Hugos egen app behåller alla flikar.

Varje ersättning är förankrad i exakt text och MÅSTE träffa exakt en gång.
Missar en av dem (för att källfilen har ändrats) avbryts bygget högljutt —
alternativet är ett paket som tyst innehåller hela appen med API-nycklar i,
och där kostar en tyst miss riktiga pengar.
"""
from __future__ import annotations

import sys
from pathlib import Path

TAB_LIST_OLD = """      <template x-for="t in [
          {slug: 'swap',   label: 'Swap'},
          {slug: 'animate', label: '🎬 Animate'},
          {slug: 'reengineer', label: '♻️ Reengineer'},
          {slug: 'editor', label: 'Editor'},
        ]" :key="t.slug">"""
TAB_LIST_NEW = """      <template x-for="t in [
          {slug: 'editor', label: 'Editor'},
        ]" :key="t.slug">"""

TAB_BAR_OLD = ('    <div class="flex items-center gap-1 border-b '
               'border-neutral-200 dark:border-neutral-800 -mb-2 '
               'overflow-x-auto whitespace-nowrap">')
TAB_BAR_NEW = ('    <div x-show="false" class="flex items-center gap-1 '
               'border-b border-neutral-200 dark:border-neutral-800 -mb-2 '
               'overflow-x-auto whitespace-nowrap">')

SIDEBAR_OLD = ('  <aside class="flex flex-col w-72 md:w-64 shrink-0 border-r '
               'border-neutral-200 dark:border-neutral-800 bg-white '
               'dark:bg-neutral-900 fixed md:static inset-y-0 left-0 z-50 '
               'transform transition-transform duration-200 md:translate-x-0"')
SIDEBAR_NEW = ('  <aside x-show="false" class="flex flex-col w-72 md:w-64 '
               'shrink-0 border-r border-neutral-200 dark:border-neutral-800 '
               'bg-white dark:bg-neutral-900 fixed md:static inset-y-0 left-0 '
               'z-50 transform transition-transform duration-200 '
               'md:translate-x-0"')

CHARLIB_BTN_OLD = """        <button @click="toggleCharLib()"
                title="Toggle character library\""""
CHARLIB_BTN_NEW = """        <button x-show="false" @click="toggleCharLib()"
                title="Toggle character library\""""

VALID_TABS_OLD = """      const _validTabs = ['swap', 'animate', 'reengineer', 'editor'];
      const _storedTab = localStorage.getItem('active_tab');
      this.activeTab = _validTabs.includes(_storedTab) ? _storedTab : 'swap';"""
VALID_TABS_NEW = """      const _validTabs = ['editor'];
      const _storedTab = localStorage.getItem('active_tab');
      this.activeTab = _validTabs.includes(_storedTab) ? _storedTab : 'editor';"""

# Hugos egen Telegram-bot ska inte stå namngiven i någon annans kopia.
# Både jämförelsen och felmeddelandet byts, så koden förblir konsekvent.
# (Character-boten går ändå inte att nå från Editor-fliken.)
BOT_NAME_OLD = "HugoCharacterSwapFinalsBot"
BOT_NAME_NEW = "CharacterSwapFinalsBot"

EDITS: list[tuple[str, str, str]] = [
    ("web/index.html", TAB_LIST_OLD, TAB_LIST_NEW),
    ("web/index.html", TAB_BAR_OLD, TAB_BAR_NEW),
    ("web/index.html", SIDEBAR_OLD, SIDEBAR_NEW),
    ("web/index.html", CHARLIB_BTN_OLD, CHARLIB_BTN_NEW),
    ("web/app.js", VALID_TABS_OLD, VALID_TABS_NEW),
]


def main(stage: Path) -> int:
    problems: list[str] = []
    for rel, old, new in EDITS:
        path = stage / rel
        text = path.read_text(encoding="utf-8")
        n = text.count(old)
        if n != 1:
            problems.append(f"{rel}: hittade {n} träffar (väntade 1) för:\n"
                            f"    {old.splitlines()[0][:90]}…")
            continue
        path.write_text(text.replace(old, new), encoding="utf-8")

    api = stage / "src/character_swap/api.py"
    api_text = api.read_text(encoding="utf-8")
    if BOT_NAME_OLD not in api_text:
        problems.append("api.py: hittade inte botnamnet som skulle bytas")
    else:
        api.write_text(api_text.replace(BOT_NAME_OLD, BOT_NAME_NEW),
                       encoding="utf-8")

    if problems:
        print("\nEditor-only-anpassningen MISSLYCKADES:", file=sys.stderr)
        for p in problems:
            print(f"  ✗ {p}", file=sys.stderr)
        print("\nKällfilerna har ändrats sedan skriptet skrevs. Uppdatera "
              "ankartexterna i packaging/editor_only.py — bygg ALDRIG vidare "
              "utan dem: paketet skulle då innehålla hela appen med "
              "API-nycklar i.\n", file=sys.stderr)
        return 1

    print(f"Editor-only: {len(EDITS) + 1} ändringar gjorda")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]).resolve()))
