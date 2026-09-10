#!/usr/bin/env python3
"""
FLP Note Merger

Create a notes-only copy of an FL Studio project in which every arranged
Pattern Clip is flattened into one merged pattern. Audio/automation clips are
not copied to the output playlists. The source .flp is never modified.

The FLP container and event stream are processed directly so note payloads can
be handled with bounded RAM. Turbo mode uses NumPy's compiled native vector
loops and direct binary writes; an external-sort compatibility path remains
available for projects containing millions of notes.

FL Studio's .flp format is proprietary and this is an unofficial tool. Always
open and verify the generated copy before relying on it.
"""

from __future__ import annotations

import argparse
import heapq
import os
import queue
import struct
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable, Optional

APP_NAME = "FLP Note Merger"
APP_VERSION = "1.2.0"

LANGUAGES = {
    "English": "en",
    "Türkçe": "tr",
    "日本語": "ja",
    "Русский": "ru",
}

TRANSLATIONS = {
    "en": {
        "ready": "Ready", "language": "Language", "subtitle": "Flatten every Playlist Pattern Clip into one lossless FL pattern. Audio and automation clips are excluded.", "split_midi": "Split MIDI by Arrangement", "split_info": "When MIDI export is enabled, create one MIDI file for each Arrangement.",
        "files": "Project files", "input": "Input .flp:", "output": "Output copy:", "browse": "Browse…", "behavior": "Merge behavior",
        "turbo": "Turbo expansion (native vector batches; recommended)", "turbo_info": "Uses NumPy's compiled native loops and bypasses the unnecessary global note sort. Turn this off only for strict sorted-record compatibility.",
        "muted": "Include muted Pattern Clips (keeps every note)", "muted_info": "Muted clip state cannot exist after everything is merged into one clip. Included muted notes will therefore become unmuted. Turn this off to preserve the audible song instead.",
        "arrangements": "All Arrangements are preserved as separate offset sections of one target pattern. Old note payloads and Pattern event automation are cleared to reduce loading and file size.",
        "midi_mode": "Export MIDI instead of a merged FLP copy", "merge": "Merge notes", "export": "Export MIDI", "cancel": "Cancel", "activity": "Activity",
        "warning": "Unofficial FLP editing tool — the original file is never changed. Verify the output in FL Studio.", "choose_project": "Choose FL Studio project", "save_midi": "Save MIDI file", "save_flp": "Save merged FLP copy", "midi_file": "MIDI file", "flp_file": "FL Studio project", "all_files": "All files",
        "choose_input": "Choose an input FLP.", "choose_output": "Choose an output path.", "different": "Input and output must be different files.", "replace": "The output already exists:\n\n{path}\n\nReplace it?", "starting": "Starting…", "mode_midi": "Mode: MIDI export", "mode_flp": "Mode: merged FLP copy", "muted_included": "included", "muted_skipped": "skipped", "engine": "Expansion engine: ", "turbo_engine": "Turbo native/direct", "sorted_engine": "Compatibility sorted", "cancel_requested": "Cancellation requested…", "cancelling": "Cancelling after the current block…", "completed": "Completed successfully.", "midi_completed": "MIDI export completed successfully.", "cancelled": "Cancelled.", "failed": "Failed.", "open_verify": "Open and verify the copy in FL Studio.", "midi_summary": "Exported {count:,} notes to MIDI.\nOutput: {output}", "flp_summary": "Merged {count:,} notes from {clips:,} Pattern Clips.\nTarget pattern: {pattern}\nOutput: {size}\nTime: {time:,.1f} seconds",
    },
    "tr": {
        "ready": "Hazır", "language": "Dil", "subtitle": "Tüm Playlist Pattern Clip'lerini kayıpsız tek bir FL pattern'ında birleştirir. Ses ve otomasyon klipleri hariç tutulur.", "split_midi": "MIDI'yi Düzenlemeye Göre Böl", "split_info": "MIDI dışa aktarımı açıkken her Düzenleme için bir MIDI dosyası oluşturur.", "files": "Proje dosyaları", "input": "Girdi .flp:", "output": "Çıktı:", "browse": "Gözat…", "behavior": "Birleştirme seçenekleri", "turbo": "Turbo genişletme (yerel vektör paketleri; önerilir)", "turbo_info": "NumPy'nin derlenmiş döngülerini kullanır ve gereksiz global nota sıralamasını atlar.", "muted": "Sessiz Pattern Clip'lerini dahil et", "muted_info": "Birleştirilmiş tek klipte sessizlik durumu korunamaz.", "arrangements": "Tüm düzenlemeler hedef pattern içinde ayrı bölümler olarak korunur.", "midi_mode": "Birleştirilmiş FLP yerine MIDI dışa aktar", "merge": "Notaları birleştir", "export": "MIDI dışa aktar", "cancel": "İptal", "activity": "Etkinlik", "warning": "Resmi olmayan FLP düzenleme aracı — orijinal dosya değiştirilmez.", "choose_project": "FL Studio projesi seç", "save_midi": "MIDI dosyasını kaydet", "save_flp": "Birleştirilmiş FLP kopyasını kaydet", "midi_file": "MIDI dosyası", "flp_file": "FL Studio projesi", "all_files": "Tüm dosyalar", "choose_input": "Bir FLP girdisi seçin.", "choose_output": "Bir çıktı yolu seçin.", "different": "Girdi ve çıktı farklı dosyalar olmalıdır.", "replace": "Çıktı zaten var:\n\n{path}\n\nDeğiştirilsin mi?", "starting": "Başlatılıyor…", "mode_midi": "Mod: MIDI dışa aktarma", "mode_flp": "Mod: birleştirilmiş FLP", "muted_included": "dahil", "muted_skipped": "atlanıyor", "engine": "Genişletme motoru: ", "turbo_engine": "Turbo yerel/doğrudan", "sorted_engine": "Uyumluluk sıralı", "cancel_requested": "İptal istendi…", "cancelling": "Mevcut bloktan sonra iptal ediliyor…", "completed": "Başarıyla tamamlandı.", "midi_completed": "MIDI dışa aktarma başarıyla tamamlandı.", "cancelled": "İptal edildi.", "failed": "Başarısız.", "open_verify": "Kopyayı FL Studio'da açıp doğrulayın.", "midi_summary": "{count:,} nota MIDI'ye aktarıldı.\nÇıktı: {output}", "flp_summary": "{clips:,} Pattern Clip'ten {count:,} nota birleştirildi.\nHedef pattern: {pattern}\nÇıktı: {size}\nSüre: {time:,.1f} saniye",
    },
    "ja": {
        "ready": "準備完了", "language": "言語", "subtitle": "すべてのプレイリストのパターンクリップを、1つのFLパターンに統合します。オーディオとオートメーションは除外されます。", "split_midi": "アレンジメントごとにMIDIを分割", "split_info": "MIDI書き出し時に、アレンジメントごとに1つのMIDIファイルを作成します。", "files": "プロジェクトファイル", "input": "入力 .flp:", "output": "出力:", "browse": "参照…", "behavior": "統合設定", "turbo": "ターボ展開（ネイティブベクトル処理・推奨）", "turbo_info": "NumPyのネイティブループを使用し、不要な全体ソートを省略します。", "muted": "ミュートされたパターンクリップを含める", "muted_info": "統合後はミュート状態を保持できません。", "arrangements": "すべてのアレンジメントは、対象パターン内の個別セクションとして保持されます。", "midi_mode": "統合FLPの代わりにMIDIを書き出す", "merge": "ノートを統合", "export": "MIDIを書き出す", "cancel": "キャンセル", "activity": "アクティビティ", "warning": "非公式のFLP編集ツール — 元のファイルは変更されません。", "choose_project": "FL Studioプロジェクトを選択", "save_midi": "MIDIファイルを保存", "save_flp": "統合FLPコピーを保存", "midi_file": "MIDIファイル", "flp_file": "FL Studioプロジェクト", "all_files": "すべてのファイル", "choose_input": "入力FLPを選択してください。", "choose_output": "出力先を選択してください。", "different": "入力と出力は別のファイルにしてください。", "replace": "出力ファイルは既に存在します:\n\n{path}\n\n置き換えますか？", "starting": "開始中…", "mode_midi": "モード: MIDI書き出し", "mode_flp": "モード: 統合FLP", "muted_included": "含める", "muted_skipped": "除外", "engine": "展開エンジン: ", "turbo_engine": "ターボ ネイティブ/直接", "sorted_engine": "互換ソート", "cancel_requested": "キャンセルを要求しました…", "cancelling": "現在のブロック後にキャンセルします…", "completed": "正常に完了しました。", "midi_completed": "MIDIの書き出しが完了しました。", "cancelled": "キャンセルしました。", "failed": "失敗しました。", "open_verify": "コピーをFL Studioで開いて確認してください。", "midi_summary": "{count:,}個のノートをMIDIに書き出しました。\n出力: {output}", "flp_summary": "{clips:,}個のPattern Clipから{count:,}個のノートを統合しました。\n対象パターン: {pattern}\n出力: {size}\n時間: {time:,.1f}秒",
    },
    "ru": {
        "ready": "Готово", "language": "Язык", "subtitle": "Объединяет все клипы паттернов Playlist в один FL-паттерн без потерь. Аудио и автоматизация исключаются.", "split_midi": "Разделять MIDI по аранжировкам", "split_info": "При экспорте MIDI создавать отдельный файл для каждой аранжировки.", "files": "Файлы проекта", "input": "Входной .flp:", "output": "Выход:", "browse": "Обзор…", "behavior": "Параметры объединения", "turbo": "Турбо-обработка (нативные векторные пакеты; рекомендуется)", "turbo_info": "Использует скомпилированные циклы NumPy и пропускает ненужную глобальную сортировку нот.", "muted": "Включать заглушенные клипы паттернов", "muted_info": "После объединения состояние заглушения сохранить нельзя.", "arrangements": "Все аранжировки сохраняются отдельными секциями внутри целевого паттерна.", "midi_mode": "Экспортировать MIDI вместо объединенного FLP", "merge": "Объединить ноты", "export": "Экспорт MIDI", "cancel": "Отмена", "activity": "Активность", "warning": "Неофициальный редактор FLP — исходный файл не изменяется.", "choose_project": "Выберите проект FL Studio", "save_midi": "Сохранить MIDI-файл", "save_flp": "Сохранить объединенную копию FLP", "midi_file": "MIDI-файл", "flp_file": "Проект FL Studio", "all_files": "Все файлы", "choose_input": "Выберите входной FLP.", "choose_output": "Выберите путь вывода.", "different": "Входной и выходной файлы должны отличаться.", "replace": "Выходной файл уже существует:\n\n{path}\n\nЗаменить его?", "starting": "Запуск…", "mode_midi": "Режим: экспорт MIDI", "mode_flp": "Режим: объединенный FLP", "muted_included": "включены", "muted_skipped": "пропущены", "engine": "Движок расширения: ", "turbo_engine": "Турбо нативный/прямой", "sorted_engine": "Совместимая сортировка", "cancel_requested": "Запрошена отмена…", "cancelling": "Отмена после текущего блока…", "completed": "Успешно завершено.", "midi_completed": "Экспорт MIDI успешно завершен.", "cancelled": "Отменено.", "failed": "Ошибка.", "open_verify": "Откройте копию в FL Studio и проверьте ее.", "midi_summary": "Экспортировано нот: {count:,}.\nВыход: {output}", "flp_summary": "Объединено нот: {count:,} из клипов: {clips:,}.\nЦелевой паттерн: {pattern}\nВыход: {size}\nВремя: {time:,.1f} с",
    },
}

TRANSLATIONS["en"].update({
    "notes_per_file": "Notes/File:",
    "notes_per_file_info": "Create numbered MIDI files with this many notes per file. Leave empty to disable.",
})
TRANSLATIONS["tr"].update({
    "notes_per_file": "Nota/Dosya:",
    "notes_per_file_info": "Bu kadar nota içeren numaralı MIDI dosyaları oluşturur. Kapatmak için boş bırakın.",
})
TRANSLATIONS["ja"].update({
    "notes_per_file": "ノート/ファイル:",
    "notes_per_file_info": "指定したノート数ごとに番号付きMIDIを作成します。無効にする場合は空欄にします。",
})
TRANSLATIONS["ru"].update({
    "notes_per_file": "Нот/файл:",
    "notes_per_file_info": "Создавать нумерованные MIDI-файлы с указанным числом нот. Оставьте пустым, чтобы отключить.",
})

# FL note fields used by MIDI export. FL stores the piano-roll key as a
# 16-bit value and velocity as an 8-bit value in each 24-byte note record.
NOTE_KEY_OFFSET = 12
NOTE_VELOCITY_OFFSET = 21

# FLP event IDs used by this tool.
EV_PLAY_TRUNCATED = 30
EV_NEW_PATTERN = 65
EV_NEW_ARRANGEMENT = 99
EV_CURRENT_ARRANGEMENT = 100
EV_PATTERN_LENGTH = 164
EV_PATTERN_NAME = 193
EV_FL_VERSION = 199
EV_PATTERN_CONTROLLERS = 223
EV_PATTERN_NOTES = 224
EV_PLAYLIST = 233

NOTE_SIZE = 24
PLAYLIST_OLD_SIZE = 32
PLAYLIST_NEW_SIZE = 60
UINT32_MAX = 0xFFFFFFFF
UINT64_MAX = 0xFFFFFFFFFFFFFFFF
INT32_MAX = 0x7FFFFFFF
PPQ_MIN = 24
PPQ_MAX = 0xFFFF  # FLhd stores PPQ as an unsigned 16-bit integer.
MUTED_CLIP_FLAG = 0x2000

CLIP_TEMP = struct.Struct("<IIIIiiB3x")
# arr_id, position, length, pattern_id, start_offset, end_offset, muted

StatusCallback = Callable[[str], None]


class MergeCancelled(Exception):
    """Raised when the user asks the worker to stop."""


class FLPFormatError(Exception):
    """Raised when a file is not a supported/valid FLP container."""


@dataclass(frozen=True)
class EventRef:
    event_start: int
    data_offset: int
    data_length: int
    event_id: int
    pattern_id: int | None = None
    arrangement_id: int | None = None


@dataclass
class ArrangementInfo:
    iid: int
    order: int
    playlist: EventRef | None = None
    record_size: int = PLAYLIST_OLD_SIZE
    template: bytes | None = None
    template_is_plain: bool = False
    pattern_clip_count: int = 0
    included_clip_count: int = 0
    timeline_length: int = 0
    section_offset: int = 0


@dataclass
class ScanResult:
    source_path: Path
    file_size: int
    header_length: int
    ppq: int
    data_length_pos: int
    data_offset: int
    data_length: int
    suffix_offset: int
    version_text: str = "unknown"
    version_major: int = 0
    current_arrangement: int = 0
    play_truncated_notes: bool = True
    arrangement_order: list[int] = field(default_factory=list)
    arrangements: dict[int, ArrangementInfo] = field(default_factory=dict)
    note_events: dict[int, list[EventRef]] = field(default_factory=dict)
    pattern_names: dict[int, str] = field(default_factory=dict)
    pattern_ids: set[int] = field(default_factory=set)
    target_pattern_id: int | None = None
    target_note_event_start: int | None = None
    source_note_count: int = 0


@dataclass
class MergeOptions:
    include_muted_clips: bool = True
    # Turbo mode writes transformed records directly. FL Studio accepts note
    # records in insertion order, so a global Python external sort is optional.
    turbo_mode: bool = True
    sort_run_records: int = 150_000
    numpy_batch_records: int = 1_000_000


@dataclass
class MergeStats:
    source_notes: int = 0
    source_pattern_clips: int = 0
    included_pattern_clips: int = 0
    merged_notes: int = 0
    arrangements: int = 0
    source_size: int = 0
    output_size: int = 0
    elapsed_seconds: float = 0.0
    target_pattern_id: int = 0


# ----------------------------- Binary helpers -----------------------------


def check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise MergeCancelled("Cancelled by user.")


def read_exact(stream: BinaryIO, count: int) -> bytes:
    data = stream.read(count)
    if len(data) != count:
        raise FLPFormatError("Unexpected end of file.")
    return data


def copy_exact(source: BinaryIO, target: BinaryIO, count: int, chunk: int = 4 * 1024 * 1024) -> None:
    remaining = count
    while remaining:
        block = source.read(min(chunk, remaining))
        if not block:
            raise FLPFormatError("Unexpected end of file while copying.")
        target.write(block)
        remaining -= len(block)


def read_varint(stream: BinaryIO, boundary: int) -> int:
    value = 0
    shift = 0
    for _ in range(10):
        if stream.tell() >= boundary:
            raise FLPFormatError("Truncated FLP variable-length event.")
        byte = read_exact(stream, 1)[0]
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value
        shift += 7
    raise FLPFormatError("FLP event length uses an invalid variable integer.")


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("A variable integer cannot be negative.")
    output = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            output.append(byte | 0x80)
        else:
            output.append(byte)
            return bytes(output)


def event_data_length(stream: BinaryIO, event_id: int, boundary: int) -> int:
    if event_id < 64:
        return 1
    if event_id < 128:
        return 2
    if event_id < 192:
        return 4
    return read_varint(stream, boundary)


def write_empty_variable_event(stream: BinaryIO, event_id: int) -> None:
    stream.write(bytes((event_id, 0)))


def write_variable_blob_event(
    output: BinaryIO,
    event_id: int,
    blob_path: Path,
    blob_size: int,
    cancel: threading.Event | None,
) -> None:
    output.write(bytes((event_id,)))
    output.write(encode_varint(blob_size))
    with blob_path.open("rb", buffering=4 * 1024 * 1024) as blob:
        remaining = blob_size
        while remaining:
            check_cancel(cancel)
            block = blob.read(min(4 * 1024 * 1024, remaining))
            if not block:
                raise FLPFormatError("Temporary merged-note file was truncated.")
            output.write(block)
            remaining -= len(block)


def decode_c_string(data: bytes) -> str:
    return data.split(b"\0", 1)[0].decode("ascii", errors="replace")


def decode_fl_text(data: bytes) -> str:
    # Modern FLP project strings are generally UTF-16LE. Fall back to UTF-8.
    try:
        if len(data) >= 2 and (data[1] == 0 or data.endswith(b"\0\0")):
            if len(data) % 2:
                data = data[:-1]
            return data.decode("utf-16le", errors="replace").rstrip("\0")
        return data.decode("utf-8", errors="replace").rstrip("\0")
    except Exception:
        return ""


def parse_version_major(text: str) -> int:
    try:
        return int(text.split(".", 1)[0])
    except (TypeError, ValueError):
        return 0


# ------------------------------- FLP scan ---------------------------------


def scan_flp(
    source_path: Path,
    status: StatusCallback,
    cancel: threading.Event | None,
) -> ScanResult:
    status("Scanning FLP structure…")
    file_size = source_path.stat().st_size
    with source_path.open("rb", buffering=4 * 1024 * 1024) as source:
        if read_exact(source, 4) != b"FLhd":
            raise FLPFormatError("Not an FL Studio project: FLhd header is missing.")
        header_length = struct.unpack("<I", read_exact(source, 4))[0]
        if header_length < 6 or header_length > 1024 * 1024:
            raise FLPFormatError(f"Unsupported FLP header size: {header_length} bytes.")
        header = read_exact(source, header_length)
        _format, _channels, ppq = struct.unpack_from("<HHH", header, 0)
        if not PPQ_MIN <= ppq <= PPQ_MAX:
            raise FLPFormatError(
                f"The FLP PPQ/timebase must be between {PPQ_MIN} and {PPQ_MAX}; found {ppq}."
            )

        if read_exact(source, 4) != b"FLdt":
            raise FLPFormatError("Not an FL Studio project: FLdt data chunk is missing.")
        data_length_pos = source.tell()
        data_length = struct.unpack("<I", read_exact(source, 4))[0]
        data_offset = source.tell()
        data_end = data_offset + data_length
        if data_end > file_size:
            raise FLPFormatError("The FLdt chunk extends past the end of the file.")

        result = ScanResult(
            source_path=source_path,
            file_size=file_size,
            header_length=header_length,
            ppq=ppq,
            data_length_pos=data_length_pos,
            data_offset=data_offset,
            data_length=data_length,
            suffix_offset=data_end,
        )

        current_pattern: int | None = None
        current_arrangement: int | None = None
        last_report = 0

        while source.tell() < data_end:
            check_cancel(cancel)
            event_start = source.tell()
            event_id = read_exact(source, 1)[0]
            length = event_data_length(source, event_id, data_end)
            data_at = source.tell()
            next_event = data_at + length
            if next_event > data_end:
                raise FLPFormatError(
                    f"Event {event_id} at offset {event_start} extends outside FLdt."
                )

            if event_id == EV_NEW_PATTERN:
                current_pattern = struct.unpack("<H", read_exact(source, 2))[0]
                result.pattern_ids.add(current_pattern)
            elif event_id == EV_NEW_ARRANGEMENT:
                current_arrangement = struct.unpack("<H", read_exact(source, 2))[0]
                if current_arrangement not in result.arrangements:
                    result.arrangement_order.append(current_arrangement)
                    result.arrangements[current_arrangement] = ArrangementInfo(
                        iid=current_arrangement,
                        order=len(result.arrangement_order) - 1,
                    )
            elif event_id == EV_CURRENT_ARRANGEMENT:
                result.current_arrangement = struct.unpack("<H", read_exact(source, 2))[0]
            elif event_id == EV_PLAY_TRUNCATED:
                result.play_truncated_notes = bool(read_exact(source, 1)[0])
            elif event_id == EV_FL_VERSION:
                raw = read_exact(source, min(length, 256))
                result.version_text = decode_c_string(raw)
                result.version_major = parse_version_major(result.version_text)
            elif event_id == EV_PATTERN_NAME and current_pattern is not None:
                raw = read_exact(source, min(length, 4096))
                result.pattern_names[current_pattern] = decode_fl_text(raw)
            elif event_id == EV_PATTERN_NOTES:
                if current_pattern is None:
                    raise FLPFormatError("Found a note event before any pattern identifier.")
                if length % NOTE_SIZE:
                    raise FLPFormatError(
                        f"Pattern {current_pattern} has a malformed note payload ({length} bytes)."
                    )
                ref = EventRef(
                    event_start=event_start,
                    data_offset=data_at,
                    data_length=length,
                    event_id=event_id,
                    pattern_id=current_pattern,
                )
                result.note_events.setdefault(current_pattern, []).append(ref)
                result.source_note_count += length // NOTE_SIZE
                if result.target_pattern_id is None and length:
                    result.target_pattern_id = current_pattern
                    result.target_note_event_start = event_start
            elif event_id == EV_PLAYLIST:
                if current_arrangement is None:
                    # Very old projects may omit an explicit arrangement marker.
                    current_arrangement = 0
                    if 0 not in result.arrangements:
                        result.arrangement_order.append(0)
                        result.arrangements[0] = ArrangementInfo(iid=0, order=0)
                ref = EventRef(
                    event_start=event_start,
                    data_offset=data_at,
                    data_length=length,
                    event_id=event_id,
                    arrangement_id=current_arrangement,
                )
                result.arrangements[current_arrangement].playlist = ref

            source.seek(next_event)
            scanned = source.tell() - data_offset
            if scanned - last_report >= 64 * 1024 * 1024:
                last_report = scanned
                status(f"Scanning FLP… {scanned / (1024**2):,.0f} MiB")

        if source.tell() != data_end:
            raise FLPFormatError("FLdt event stream did not end on an event boundary.")

    if not result.arrangements:
        # A pattern-only project still gets a synthetic empty arrangement entry.
        result.arrangement_order.append(0)
        result.arrangements[0] = ArrangementInfo(iid=0, order=0)

    if result.target_pattern_id is None or result.target_note_event_start is None:
        raise FLPFormatError(
            "No piano-roll/step-sequencer notes were found. A project with at least one note is required."
        )

    status(
        f"Found {result.source_note_count:,} stored notes in "
        f"{len(result.note_events):,} patterns at {result.ppq:,} PPQ."
    )
    return result


# -------------------------- Playlist extraction ---------------------------


def choose_playlist_record_size(scan: ScanResult, payload_length: int) -> int:
    if payload_length == 0:
        return PLAYLIST_NEW_SIZE if scan.version_major >= 21 else PLAYLIST_OLD_SIZE
    if scan.version_major >= 21 and payload_length % PLAYLIST_NEW_SIZE == 0:
        return PLAYLIST_NEW_SIZE
    if payload_length % PLAYLIST_OLD_SIZE == 0:
        return PLAYLIST_OLD_SIZE
    if payload_length % PLAYLIST_NEW_SIZE == 0:
        return PLAYLIST_NEW_SIZE
    raise FLPFormatError(
        f"Unsupported Playlist payload size ({payload_length} bytes) for FL Studio "
        f"{scan.version_text}."
    )


def extract_pattern_clips(
    scan: ScanResult,
    clip_path: Path,
    options: MergeOptions,
    status: StatusCallback,
    cancel: threading.Event | None,
) -> tuple[int, int]:
    status("Indexing Pattern Clips (audio and automation are ignored)…")
    total_pattern_clips = 0
    included_pattern_clips = 0

    with scan.source_path.open("rb", buffering=4 * 1024 * 1024) as source, clip_path.open(
        "wb", buffering=4 * 1024 * 1024
    ) as clip_file:
        for arr_id in scan.arrangement_order:
            check_cancel(cancel)
            arr = scan.arrangements[arr_id]
            ref = arr.playlist
            if ref is None or ref.data_length == 0:
                continue
            record_size = choose_playlist_record_size(scan, ref.data_length)
            arr.record_size = record_size
            source.seek(ref.data_offset)
            records = ref.data_length // record_size

            for index in range(records):
                if index % 50_000 == 0:
                    check_cancel(cancel)
                raw = read_exact(source, record_size)
                position, pattern_base, item_index, length = struct.unpack_from("<IHHI", raw, 0)
                if item_index <= pattern_base:
                    continue  # Audio Clip or Automation Clip.

                total_pattern_clips += 1
                pattern_id = item_index - pattern_base
                flags = struct.unpack_from("<H", raw, 18)[0]
                muted = bool(flags & MUTED_CLIP_FLAG)
                start_offset, end_offset = struct.unpack_from("<ii", raw, 24)

                # Prefer a normal, non-stretched source record as the skeleton
                # for the replacement clip. This avoids carrying clip-specific
                # stretch/variant state from an unusual first item.
                plain_template = (
                    (start_offset < 0 and end_offset < 0)
                    or (
                        start_offset >= 0
                        and end_offset >= start_offset
                        and end_offset - start_offset == length
                    )
                )
                if arr.template is None or (plain_template and not arr.template_is_plain):
                    arr.template = raw
                    arr.template_is_plain = plain_template
                arr.pattern_clip_count += 1
                arr.timeline_length = max(arr.timeline_length, position + length)

                if muted and not options.include_muted_clips:
                    continue
                if length == 0:
                    continue

                clip_file.write(
                    CLIP_TEMP.pack(
                        arr_id,
                        position,
                        length,
                        pattern_id,
                        start_offset,
                        end_offset,
                        1 if muted else 0,
                    )
                )
                arr.included_clip_count += 1
                included_pattern_clips += 1

            status(
                f"Arrangement {arr_id}: {arr.included_clip_count:,} Pattern Clips included."
            )

    if included_pattern_clips == 0:
        reason = " after muted clips were skipped" if not options.include_muted_clips else ""
        raise FLPFormatError(f"No usable Pattern Clips were found in any Playlist{reason}.")

    # Put every arrangement in a non-overlapping section of the one target
    # pattern. Each arrangement's replacement clip uses start/end offsets to
    # expose only its own section.
    cursor = 0
    bar = max(1, scan.ppq * 4)
    for arr_id in scan.arrangement_order:
        arr = scan.arrangements[arr_id]
        if cursor % bar:
            cursor += bar - (cursor % bar)
        arr.section_offset = cursor
        cursor += arr.timeline_length
        if arr.timeline_length:
            cursor += bar  # one safety bar between arrangements
        if cursor > INT32_MAX:
            raise FLPFormatError(
                "The combined arrangements exceed FL Studio's signed Pattern Clip offset range."
            )

    return total_pattern_clips, included_pattern_clips


# ------------------------------ Run sorting -------------------------------


class ExternalNoteSorter:
    """Bounded-memory sorter for fixed 24-byte FL note records."""

    def __init__(
        self,
        temp_dir: Path,
        max_records: int,
        status: StatusCallback,
        cancel: threading.Event | None,
    ) -> None:
        self.temp_dir = temp_dir
        self.max_records = max(10_000, max_records)
        self.status = status
        self.cancel = cancel
        self.buffer: list[bytes] = []
        self.runs: list[Path] = []
        self.total_records = 0
        self._run_serial = 0

    @staticmethod
    def position(record: bytes) -> int:
        return struct.unpack_from("<I", record, 0)[0]

    def add(self, record: bytes) -> None:
        if len(record) != NOTE_SIZE:
            raise ValueError("A note record must be exactly 24 bytes.")
        self.buffer.append(record)
        self.total_records += 1
        if len(self.buffer) >= self.max_records:
            self.flush_run()

    def flush_run(self) -> None:
        if not self.buffer:
            return
        check_cancel(self.cancel)
        self.buffer.sort(key=self.position)
        path = self.temp_dir / f"notes_run_{self._run_serial:06d}.bin"
        self._run_serial += 1
        with path.open("wb", buffering=4 * 1024 * 1024) as output:
            output.writelines(self.buffer)
        self.runs.append(path)
        self.buffer.clear()
        self.status(
            f"Expanded {self.total_records:,} notes; wrote sorted run {len(self.runs):,}."
        )

    def _merge_group(self, inputs: list[Path], output_path: Path) -> None:
        streams: list[BinaryIO] = []
        try:
            streams = [path.open("rb", buffering=1024 * 1024) for path in inputs]
            # Run index is the secondary key. Runs are created in input order,
            # so equal-position notes keep deterministic/stable ordering across
            # run boundaries (useful for simultaneous slide and regular notes).
            heap: list[tuple[int, int, bytes]] = []
            for index, stream in enumerate(streams):
                record = stream.read(NOTE_SIZE)
                if record:
                    if len(record) != NOTE_SIZE:
                        raise FLPFormatError("A temporary sorted run is truncated.")
                    heapq.heappush(heap, (self.position(record), index, record))

            write_buffer = bytearray()
            with output_path.open("wb", buffering=4 * 1024 * 1024) as output:
                written = 0
                while heap:
                    if written % 100_000 == 0:
                        check_cancel(self.cancel)
                    _position, stream_index, record = heapq.heappop(heap)
                    write_buffer.extend(record)
                    written += 1
                    if len(write_buffer) >= 2 * 1024 * 1024:
                        output.write(write_buffer)
                        write_buffer.clear()

                    next_record = streams[stream_index].read(NOTE_SIZE)
                    if next_record:
                        if len(next_record) != NOTE_SIZE:
                            raise FLPFormatError("A temporary sorted run is truncated.")
                        heapq.heappush(
                            heap,
                            (self.position(next_record), stream_index, next_record),
                        )
                if write_buffer:
                    output.write(write_buffer)
        finally:
            for stream in streams:
                stream.close()

    def finish(self) -> Path:
        self.flush_run()
        if not self.runs:
            empty = self.temp_dir / "merged_notes.bin"
            empty.touch()
            return empty
        if len(self.runs) == 1:
            final = self.temp_dir / "merged_notes.bin"
            os.replace(self.runs[0], final)
            self.runs = [final]
            return final

        generation = 0
        runs = self.runs
        # Limit simultaneously open files; perform additional merge passes if needed.
        while len(runs) > 1:
            check_cancel(self.cancel)
            next_runs: list[Path] = []
            groups = [runs[i : i + 64] for i in range(0, len(runs), 64)]
            for group_index, group in enumerate(groups):
                check_cancel(self.cancel)
                path = self.temp_dir / f"merge_{generation:03d}_{group_index:06d}.bin"
                self.status(
                    f"External sort pass {generation + 1}: group {group_index + 1}/{len(groups)}…"
                )
                self._merge_group(group, path)
                next_runs.append(path)
                for old in group:
                    try:
                        old.unlink()
                    except FileNotFoundError:
                        pass
            runs = next_runs
            generation += 1

        final = self.temp_dir / "merged_notes.bin"
        os.replace(runs[0], final)
        self.runs = [final]
        return final


# ------------------------------ Note merge --------------------------------


def make_adjusted_note(
    raw: bytes,
    clip_position: int,
    clip_length: int,
    source_start: int,
    source_end: int,
    section_offset: int,
    play_truncated: bool,
) -> bytes | None:
    note_position = struct.unpack_from("<I", raw, 0)[0]
    note_length = struct.unpack_from("<I", raw, 8)[0]
    source_span = source_end - source_start
    if source_span <= 0 or clip_length <= 0:
        return None

    def map_source_delta(delta: int) -> int:
        # Pattern Clip stretching is represented by a source offset range that
        # differs from its Playlist length. Integer half-up rounding keeps the
        # first and last ticks exact and avoids cumulative floating-point error.
        return (delta * clip_length + source_span // 2) // source_span

    if note_length == 0:
        if not (source_start <= note_position < source_end):
            return None
        visible_start = note_position
        new_length = 0
    else:
        note_end = note_position + note_length

        # A clip controls which note-on events are visible, but a note whose
        # start is inside the clip must retain its complete tail. The previous
        # implementation incorrectly cut every duration at source_end; this
        # was especially visible in projects made from long red note blocks.
        if note_position >= source_end or note_end <= source_start:
            return None
        if note_position < source_start:
            if not play_truncated:
                return None
            # FL's "Play truncated notes in clips" restores the portion after
            # a sliced/cropped left edge. Only that left portion is removed.
            visible_start = source_start
        else:
            visible_start = note_position

        mapped_start = map_source_delta(visible_start - source_start)
        mapped_end = map_source_delta(note_end - source_start)
        # Do not clamp mapped_end to the Pattern Clip's right edge. Note tails
        # are allowed to continue beyond it, exactly as in the source pattern.
        new_length = max(1, mapped_end - mapped_start)

    mapped_position = map_source_delta(visible_start - source_start)
    new_position = section_offset + clip_position + mapped_position
    if not 0 <= new_position <= UINT32_MAX:
        raise FLPFormatError("A merged note position exceeds FL Studio's 32-bit range.")
    if not 0 <= new_length <= UINT32_MAX:
        raise FLPFormatError("A merged note length exceeds FL Studio's 32-bit range.")

    adjusted = bytearray(raw)
    struct.pack_into("<I", adjusted, 0, new_position)
    struct.pack_into("<I", adjusted, 8, new_length)
    return bytes(adjusted)


def build_merged_notes_sorted(
    scan: ScanResult,
    clip_path: Path,
    temp_dir: Path,
    options: MergeOptions,
    status: StatusCallback,
    cancel: threading.Event | None,
) -> tuple[Path, int]:
    sorter = ExternalNoteSorter(
        temp_dir=temp_dir,
        max_records=options.sort_run_records,
        status=status,
        cancel=cancel,
    )
    clip_size = clip_path.stat().st_size
    if clip_size % CLIP_TEMP.size:
        raise FLPFormatError("Temporary Pattern Clip index is malformed.")
    clip_count = clip_size // CLIP_TEMP.size

    status("Flattening Pattern Clips into absolute note positions…")
    with scan.source_path.open("rb", buffering=4 * 1024 * 1024) as source, clip_path.open(
        "rb", buffering=4 * 1024 * 1024
    ) as clips:
        for clip_index in range(clip_count):
            if clip_index % 1000 == 0:
                check_cancel(cancel)
                status(
                    f"Flattening clip {clip_index + 1:,}/{clip_count:,}; "
                    f"notes produced: {sorter.total_records:,}"
                )
            raw_clip = read_exact(clips, CLIP_TEMP.size)
            (
                arr_id,
                clip_position,
                clip_length,
                pattern_id,
                start_offset,
                end_offset,
                _muted,
            ) = CLIP_TEMP.unpack(raw_clip)
            arr = scan.arrangements.get(arr_id)
            if arr is None:
                raise FLPFormatError(f"Temporary clip refers to unknown arrangement {arr_id}.")
            source_start = 0 if start_offset < 0 else start_offset
            source_end = (
                end_offset
                if end_offset > source_start
                else source_start + clip_length
            )

            for note_event in scan.note_events.get(pattern_id, ()):
                source.seek(note_event.data_offset)
                records = note_event.data_length // NOTE_SIZE
                for note_index in range(records):
                    if note_index and note_index % 250_000 == 0:
                        check_cancel(cancel)
                    raw_note = read_exact(source, NOTE_SIZE)
                    adjusted = make_adjusted_note(
                        raw_note,
                        clip_position=clip_position,
                        clip_length=clip_length,
                        source_start=source_start,
                        source_end=source_end,
                        section_offset=arr.section_offset,
                        play_truncated=scan.play_truncated_notes,
                    )
                    if adjusted is not None:
                        sorter.add(adjusted)

    status(f"Sorting {sorter.total_records:,} merged notes…")
    merged_path = sorter.finish()
    expected_size = sorter.total_records * NOTE_SIZE
    if merged_path.stat().st_size != expected_size:
        raise FLPFormatError("Internal error: merged-note temporary size is incorrect.")
    return merged_path, sorter.total_records


def transform_note_block_scalar(
    raw_block: bytes,
    clip_position: int,
    clip_length: int,
    source_start: int,
    source_end: int,
    section_offset: int,
    play_truncated: bool,
) -> tuple[bytes, int]:
    """Correct bounded-memory fallback for a complete binary batch."""
    if len(raw_block) % NOTE_SIZE:
        raise FLPFormatError("A note batch is not aligned to 24-byte records.")
    output = bytearray()
    count = 0
    for offset in range(0, len(raw_block), NOTE_SIZE):
        adjusted = make_adjusted_note(
            raw_block[offset : offset + NOTE_SIZE],
            clip_position=clip_position,
            clip_length=clip_length,
            source_start=source_start,
            source_end=source_end,
            section_offset=section_offset,
            play_truncated=play_truncated,
        )
        if adjusted is not None:
            output.extend(adjusted)
            count += 1
    return bytes(output), count


def transform_note_block_numpy(
    np: object,
    raw_block: bytes,
    clip_position: int,
    clip_length: int,
    source_start: int,
    source_end: int,
    section_offset: int,
    play_truncated: bool,
) -> tuple[bytes, int]:
    """Transform a batch with NumPy's compiled native loops.

    Only position and length fields are changed. Every other byte in each
    24-byte FL note record is copied exactly.
    """
    if not raw_block:
        return b"", 0
    if len(raw_block) % NOTE_SIZE:
        raise FLPFormatError("A note batch is not aligned to 24-byte records.")
    source_span = source_end - source_start
    if source_span <= 0 or clip_length <= 0:
        return b"", 0

    # Local import/type use is intentional: NumPy is an optional turbo
    # accelerator, while the scalar engine remains a complete fallback.
    dtype = np.dtype(
        {
            "names": ("position", "length"),
            "formats": ("<u4", "<u4"),
            "offsets": (0, 8),
            "itemsize": NOTE_SIZE,
        }
    )
    records = np.frombuffer(raw_block, dtype=dtype)
    positions = records["position"].astype(np.uint64, copy=False)
    lengths = records["length"].astype(np.uint64, copy=False)
    source_start_u = np.uint64(source_start)
    source_end_u = np.uint64(source_end)

    zero_length = lengths == 0
    note_ends = positions + lengths
    eligible_zero = zero_length & (positions >= source_start_u) & (positions < source_end_u)
    eligible_notes = (~zero_length) & (positions < source_end_u) & (note_ends > source_start_u)
    if not play_truncated:
        eligible_notes &= positions >= source_start_u
    eligible = eligible_zero | eligible_notes
    indexes = np.flatnonzero(eligible)
    count = int(indexes.size)
    if count == 0:
        return b"", 0

    selected_positions = positions[indexes]
    selected_lengths = lengths[indexes]
    selected_ends = note_ends[indexes]
    visible_starts = np.maximum(selected_positions, source_start_u)
    span_u = np.uint64(source_span)
    clip_length_u = np.uint64(clip_length)
    rounding_value = source_span // 2
    rounding = np.uint64(rounding_value)

    # uint64 vector multiplication is extremely fast but must not wrap for a
    # pathological maximum-position + maximum-length note. Rare unsafe batches
    # fall back to Python's arbitrary-precision integer implementation.
    max_delta = int(np.max(selected_ends - source_start_u))
    safe_delta = (UINT64_MAX - rounding_value) // clip_length
    if max_delta > safe_delta:
        return transform_note_block_scalar(
            raw_block,
            clip_position,
            clip_length,
            source_start,
            source_end,
            section_offset,
            play_truncated,
        )

    def map_deltas(deltas: object) -> object:
        return (deltas * clip_length_u + rounding) // span_u

    mapped_starts = map_deltas(visible_starts - source_start_u)
    new_positions = mapped_starts + np.uint64(section_offset + clip_position)
    new_lengths = np.zeros(count, dtype=np.uint64)
    positive_indexes = np.flatnonzero(selected_lengths != 0)
    if positive_indexes.size:
        mapped_ends = map_deltas(selected_ends[positive_indexes] - source_start_u)
        durations = mapped_ends - mapped_starts[positive_indexes]
        new_lengths[positive_indexes] = np.maximum(durations, np.uint64(1))

    if bool(np.any(new_positions > UINT32_MAX)):
        raise FLPFormatError("A merged note position exceeds FL Studio's 32-bit range.")
    if bool(np.any(new_lengths > UINT32_MAX)):
        raise FLPFormatError("A merged note length exceeds FL Studio's 32-bit range.")

    # Boolean/fancy indexing copies complete raw rows in one native operation.
    rows = np.frombuffer(raw_block, dtype=np.uint8).reshape((-1, NOTE_SIZE))
    output_rows = rows[indexes].copy(order="C")
    output_positions = np.ndarray(
        shape=(count,), dtype="<u4", buffer=output_rows, offset=0, strides=(NOTE_SIZE,)
    )
    output_lengths = np.ndarray(
        shape=(count,), dtype="<u4", buffer=output_rows, offset=8, strides=(NOTE_SIZE,)
    )
    output_positions[:] = new_positions
    output_lengths[:] = new_lengths
    return output_rows.tobytes(order="C"), count


def build_merged_notes_turbo(
    scan: ScanResult,
    clip_path: Path,
    temp_dir: Path,
    options: MergeOptions,
    status: StatusCallback,
    cancel: threading.Event | None,
) -> tuple[Path, int]:
    """Fast expansion path: native vector batches and no global re-sort."""
    clip_size = clip_path.stat().st_size
    if clip_size % CLIP_TEMP.size:
        raise FLPFormatError("Temporary Pattern Clip index is malformed.")
    clip_count = clip_size // CLIP_TEMP.size
    merged_path = temp_dir / "merged_notes.bin"
    total_records = 0

    try:
        import numpy as np
    except ImportError:
        np = None
        status("Turbo mode: NumPy unavailable; using optimized scalar streaming.")
    else:
        status(
            f"Turbo mode: NumPy {np.__version__} native vector expansion; global sort bypassed."
        )

    scalar_buffer = bytearray()
    batch_records = max(10_000, int(options.numpy_batch_records))
    status("Turbo-expanding Pattern Clips into absolute note positions…")

    with scan.source_path.open("rb", buffering=8 * 1024 * 1024) as source, clip_path.open(
        "rb", buffering=4 * 1024 * 1024
    ) as clips, merged_path.open("wb", buffering=8 * 1024 * 1024) as output:
        for clip_index in range(clip_count):
            if clip_index % 100 == 0:
                check_cancel(cancel)
                status(
                    f"Turbo clip {clip_index + 1:,}/{clip_count:,}; "
                    f"notes produced: {total_records:,}"
                )
            (
                arr_id,
                clip_position,
                clip_length,
                pattern_id,
                start_offset,
                end_offset,
                _muted,
            ) = CLIP_TEMP.unpack(read_exact(clips, CLIP_TEMP.size))
            arr = scan.arrangements.get(arr_id)
            if arr is None:
                raise FLPFormatError(f"Temporary clip refers to unknown arrangement {arr_id}.")
            source_start = 0 if start_offset < 0 else start_offset
            source_end = end_offset if end_offset > source_start else source_start + clip_length

            for note_event in scan.note_events.get(pattern_id, ()):
                source.seek(note_event.data_offset)
                records_remaining = note_event.data_length // NOTE_SIZE
                if np is not None:
                    while records_remaining:
                        check_cancel(cancel)
                        take = min(batch_records, records_remaining)
                        raw_block = read_exact(source, take * NOTE_SIZE)
                        transformed, produced = transform_note_block_numpy(
                            np,
                            raw_block,
                            clip_position=clip_position,
                            clip_length=clip_length,
                            source_start=source_start,
                            source_end=source_end,
                            section_offset=arr.section_offset,
                            play_truncated=scan.play_truncated_notes,
                        )
                        if transformed:
                            output.write(transformed)
                        total_records += produced
                        records_remaining -= take
                else:
                    while records_remaining:
                        check_cancel(cancel)
                        take = min(batch_records, records_remaining)
                        transformed, produced = transform_note_block_scalar(
                            read_exact(source, take * NOTE_SIZE),
                            clip_position=clip_position,
                            clip_length=clip_length,
                            source_start=source_start,
                            source_end=source_end,
                            section_offset=arr.section_offset,
                            play_truncated=scan.play_truncated_notes,
                        )
                        scalar_buffer.extend(transformed)
                        total_records += produced
                        records_remaining -= take
                        if len(scalar_buffer) >= 4 * 1024 * 1024:
                            output.write(scalar_buffer)
                            scalar_buffer.clear()
        if scalar_buffer:
            output.write(scalar_buffer)

    expected_size = total_records * NOTE_SIZE
    if merged_path.stat().st_size != expected_size:
        raise FLPFormatError("Internal error: turbo merged-note size is incorrect.")
    status(f"Turbo expansion complete: {total_records:,} notes; no external sort needed.")
    return merged_path, total_records


def build_merged_notes(
    scan: ScanResult,
    clip_path: Path,
    temp_dir: Path,
    options: MergeOptions,
    status: StatusCallback,
    cancel: threading.Event | None,
) -> tuple[Path, int]:
    if options.turbo_mode:
        return build_merged_notes_turbo(
            scan, clip_path, temp_dir, options, status, cancel
        )
    status("Compatibility mode: globally sorting all merged note records.")
    return build_merged_notes_sorted(
        scan, clip_path, temp_dir, options, status, cancel
    )


# ------------------------------- FLP write --------------------------------


def replacement_playlist_record(
    scan: ScanResult,
    arr: ArrangementInfo,
    target_pattern_id: int,
) -> bytes:
    if arr.template is None:
        return b""
    raw = bytearray(arr.template)
    pattern_base = struct.unpack_from("<H", raw, 4)[0]
    item_index = pattern_base + target_pattern_id
    if item_index > 0xFFFF:
        raise FLPFormatError("Target pattern identifier cannot fit in a Playlist record.")
    if arr.timeline_length > UINT32_MAX:
        raise FLPFormatError("An arrangement timeline exceeds FL Studio's 32-bit range.")
    section_end = arr.section_offset + arr.timeline_length
    if section_end > INT32_MAX:
        raise FLPFormatError("A merged arrangement section exceeds the Pattern Clip offset range.")

    struct.pack_into("<I", raw, 0, 0)  # Playlist position
    struct.pack_into("<H", raw, 6, item_index)
    struct.pack_into("<I", raw, 8, arr.timeline_length)
    struct.pack_into("<H", raw, 14, 0)  # no clip group
    flags = struct.unpack_from("<H", raw, 18)[0] & ~MUTED_CLIP_FLAG
    struct.pack_into("<H", raw, 18, flags)
    struct.pack_into("<ii", raw, 24, arr.section_offset, section_end)
    return bytes(raw)


def rewrite_project(
    scan: ScanResult,
    merged_notes_path: Path,
    merged_note_count: int,
    output_path: Path,
    status: StatusCallback,
    cancel: threading.Event | None,
) -> None:
    target_pattern = scan.target_pattern_id
    target_event_start = scan.target_note_event_start
    assert target_pattern is not None and target_event_start is not None

    note_blob_size = merged_note_count * NOTE_SIZE
    if note_blob_size > UINT32_MAX:
        raise FLPFormatError(
            "Merged note data exceeds the 4 GiB FLdt chunk limit. Split the project first."
        )

    playlist_replacements = {
        arr_id: replacement_playlist_record(scan, scan.arrangements[arr_id], target_pattern)
        for arr_id in scan.arrangement_order
    }

    status("Writing optimized FLP copy…")
    partial = output_path.with_name(output_path.name + ".partial")
    try:
        with scan.source_path.open("rb", buffering=4 * 1024 * 1024) as source, partial.open(
            "w+b", buffering=4 * 1024 * 1024
        ) as output:
            # Copy FLhd and the FLdt marker, then reserve the FLdt length field.
            source.seek(0)
            copy_exact(source, output, scan.data_length_pos)
            new_data_length_pos = output.tell()
            output.write(b"\0\0\0\0")
            new_data_start = output.tell()

            source.seek(scan.data_offset)
            data_end = scan.data_offset + scan.data_length
            current_pattern: int | None = None
            current_arrangement: int | None = None
            replaced_target_notes = False
            events_seen = 0

            while source.tell() < data_end:
                check_cancel(cancel)
                event_start = source.tell()
                event_id = read_exact(source, 1)[0]
                length = event_data_length(source, event_id, data_end)
                data_at = source.tell()
                next_event = data_at + length
                if next_event > data_end:
                    raise FLPFormatError("An event extends beyond FLdt during rewrite.")

                if event_id == EV_NEW_PATTERN:
                    current_pattern = struct.unpack("<H", read_exact(source, 2))[0]
                elif event_id == EV_NEW_ARRANGEMENT:
                    current_arrangement = struct.unpack("<H", read_exact(source, 2))[0]

                if event_id == EV_PATTERN_NOTES:
                    if event_start == target_event_start:
                        write_variable_blob_event(
                            output,
                            EV_PATTERN_NOTES,
                            merged_notes_path,
                            note_blob_size,
                            cancel,
                        )
                        replaced_target_notes = True
                    else:
                        # Old pattern notes are now redundant. Emptying them is the
                        # main file-size/loading optimization.
                        write_empty_variable_event(output, EV_PATTERN_NOTES)
                elif event_id == EV_PATTERN_CONTROLLERS:
                    # The requested result is notes-only. Pattern event automation
                    # would no longer line up after flattening.
                    write_empty_variable_event(output, EV_PATTERN_CONTROLLERS)
                elif event_id == EV_PLAYLIST:
                    replacement = playlist_replacements.get(current_arrangement or 0, b"")
                    output.write(bytes((EV_PLAYLIST,)))
                    output.write(encode_varint(len(replacement)))
                    output.write(replacement)
                elif event_id == EV_PATTERN_LENGTH and current_pattern == target_pattern:
                    # Auto length: FL Studio derives it from the merged notes.
                    output.write(bytes((EV_PATTERN_LENGTH,)))
                    output.write(b"\0\0\0\0")
                else:
                    source.seek(event_start)
                    copy_exact(source, output, next_event - event_start)

                source.seek(next_event)
                events_seen += 1
                if events_seen % 100_000 == 0:
                    status(f"Writing FLP… {events_seen:,} events copied.")

            if not replaced_target_notes:
                raise FLPFormatError("Internal error: target note event was not replaced.")

            new_data_end = output.tell()
            new_data_length = new_data_end - new_data_start
            if new_data_length > UINT32_MAX:
                raise FLPFormatError(
                    "The generated FLdt chunk is larger than FLP's 4 GiB length field."
                )

            # Preserve any nonstandard bytes/chunks following FLdt.
            source.seek(scan.suffix_offset)
            suffix_size = scan.file_size - scan.suffix_offset
            if suffix_size:
                copy_exact(source, output, suffix_size)

            output.seek(new_data_length_pos)
            output.write(struct.pack("<I", new_data_length))
            output.flush()
            os.fsync(output.fileno())

        check_cancel(cancel)
        os.replace(partial, output_path)
    except Exception:
        try:
            partial.unlink()
        except FileNotFoundError:
            pass
        raise


# ---------------------------- Public operation ----------------------------


def merge_flp(
    source_path: Path,
    output_path: Path,
    options: MergeOptions | None = None,
    status: StatusCallback | None = None,
    cancel: threading.Event | None = None,
) -> MergeStats:
    options = options or MergeOptions()
    status = status or (lambda _message: None)
    source_path = source_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()

    if source_path == output_path:
        raise ValueError("Choose a different output path; the source is never overwritten.")
    if source_path.suffix.lower() != ".flp":
        raise ValueError("The input must be an .flp file.")
    if not source_path.is_file():
        raise FileNotFoundError(f"Input project not found: {source_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    scan = scan_flp(source_path, status, cancel)

    # Keep temporary files beside the system temp location. They are cleaned on
    # success, error, and cancellation.
    with tempfile.TemporaryDirectory(prefix="flp_note_merger_") as temp_name:
        temp_dir = Path(temp_name)
        clip_path = temp_dir / "pattern_clips.bin"
        total_clips, included_clips = extract_pattern_clips(
            scan, clip_path, options, status, cancel
        )
        merged_path, merged_count = build_merged_notes(
            scan, clip_path, temp_dir, options, status, cancel
        )
        if merged_count == 0:
            raise FLPFormatError(
                "Pattern Clips were found, but no notes fall inside their visible ranges."
            )
        rewrite_project(
            scan,
            merged_path,
            merged_count,
            output_path,
            status,
            cancel,
        )

    elapsed = time.monotonic() - started
    stats = MergeStats(
        source_notes=scan.source_note_count,
        source_pattern_clips=total_clips,
        included_pattern_clips=included_clips,
        merged_notes=merged_count,
        arrangements=len(scan.arrangements),
        source_size=scan.file_size,
        output_size=output_path.stat().st_size,
        elapsed_seconds=elapsed,
        target_pattern_id=scan.target_pattern_id or 0,
    )
    status(
        f"Done: {stats.merged_notes:,} notes in Pattern {stats.target_pattern_id}; "
        f"output {stats.output_size / (1024**2):,.1f} MiB."
    )
    return stats


# ------------------------------ MIDI export -------------------------------


def midi_varlen(value: int) -> bytes:
    if value < 0:
        raise ValueError("MIDI delta time cannot be negative.")
    encoded = bytearray((value & 0x7F,))
    value >>= 7
    while value:
        encoded.insert(0, (value & 0x7F) | 0x80)
        value >>= 7
    return bytes(encoded)


def write_midi_file(
    merged_notes_path: Path,
    merged_note_count: int,
    ppq: int,
    output_path: Path,
    cancel: threading.Event | None,
) -> None:
    try:
        import numpy as np
    except ImportError:
        np = None

    if np is not None:
        status = None
        note_dtype = np.dtype(
            {
                "names": ("position", "length", "key", "velocity"),
                "formats": ("<u4", "<u4", "<u2", "u1"),
                "offsets": (0, 8, NOTE_KEY_OFFSET, NOTE_VELOCITY_OFFSET),
                "itemsize": NOTE_SIZE,
            }
        )
        notes = np.fromfile(merged_notes_path, dtype=note_dtype, count=merged_note_count)
        if len(notes) != merged_note_count:
            raise FLPFormatError("Merged-note temporary file is truncated.")
        events = np.empty(
            merged_note_count * 2,
            dtype=np.dtype(
                [("tick", "<u8"), ("kind", "u1"), ("pitch", "u1"), ("velocity", "u1")]
            ),
        )
        positions = notes["position"].astype(np.uint64, copy=False)
        ends = positions + np.maximum(notes["length"], 1).astype(np.uint64, copy=False)
        pitches = np.minimum(notes["key"], 127).astype(np.uint8, copy=False)
        velocities = np.maximum(np.minimum(notes["velocity"], 127), 1)
        events[0::2]["tick"] = positions
        events[0::2]["kind"] = 1
        events[0::2]["pitch"] = pitches
        events[0::2]["velocity"] = velocities
        events[1::2]["tick"] = ends
        events[1::2]["kind"] = 0
        events[1::2]["pitch"] = pitches
        events[1::2]["velocity"] = 0
        events.sort(order=("tick", "kind"), kind="stable")
        event_rows = ((int(event["tick"]), int(event["kind"]), int(event["pitch"]), int(event["velocity"])) for event in events)
    else:
        events_fallback: list[tuple[int, int, int, int]] = []
        with merged_notes_path.open("rb", buffering=8 * 1024 * 1024) as notes:
            for index in range(merged_note_count):
                if index % 100_000 == 0:
                    check_cancel(cancel)
                raw = read_exact(notes, NOTE_SIZE)
                position = struct.unpack_from("<I", raw, 0)[0]
                length = struct.unpack_from("<I", raw, 8)[0]
                pitch = max(0, min(127, struct.unpack_from("<H", raw, NOTE_KEY_OFFSET)[0]))
                velocity = max(1, min(127, raw[NOTE_VELOCITY_OFFSET]))
                end = position + max(1, length)
                events_fallback.append((position, 1, pitch, velocity))
                events_fallback.append((end, 0, pitch, 0))
        events_fallback.sort(key=lambda event: (event[0], event[1]))
        event_rows = iter(events_fallback)

    write_midi_events(event_rows, ppq, output_path)


def write_midi_events(
    event_rows: object,
    ppq: int,
    output_path: Path,
) -> None:
    track = bytearray(b"\x00\xFF\x51\x03\x07\xA1\x20")  # 120 BPM.
    previous_tick = 0
    for tick, kind, pitch, velocity in event_rows:
        track.extend(midi_varlen(tick - previous_tick))
        track.extend((0x90 if kind else 0x80, pitch, velocity))
        previous_tick = tick
    track.extend(b"\x00\xFF\x2F\x00")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as output:
        output.write(b"MThd")
        output.write(struct.pack(">IHHH", 6, 0, 1, ppq))
        output.write(b"MTrk")
        output.write(struct.pack(">I", len(track)))
        output.write(track)


def write_split_midi_files(
    merged_notes_path: Path,
    merged_note_count: int,
    scan: ScanResult,
    output_path: Path,
    cancel: threading.Event | None,
) -> int:
    arrangement_events: dict[int, list[tuple[int, int, int, int]]] = {
        arr_id: [] for arr_id in scan.arrangement_order
    }
    with merged_notes_path.open("rb", buffering=8 * 1024 * 1024) as notes:
        for index in range(merged_note_count):
            if index % 100_000 == 0:
                check_cancel(cancel)
            raw = read_exact(notes, NOTE_SIZE)
            position = struct.unpack_from("<I", raw, 0)[0]
            length = struct.unpack_from("<I", raw, 8)[0]
            for arr_id in scan.arrangement_order:
                arr = scan.arrangements[arr_id]
                if arr.section_offset <= position < arr.section_offset + arr.timeline_length:
                    relative_position = position - arr.section_offset
                    pitch = max(0, min(127, struct.unpack_from("<H", raw, NOTE_KEY_OFFSET)[0]))
                    velocity = max(1, min(127, raw[NOTE_VELOCITY_OFFSET]))
                    arrangement_events[arr_id].append(
                        (relative_position, 1, pitch, velocity)
                    )
                    arrangement_events[arr_id].append(
                        (relative_position + max(1, length), 0, pitch, 0)
                    )
                    break

    for arr_id in scan.arrangement_order:
        check_cancel(cancel)
        events = arrangement_events[arr_id]
        events.sort(key=lambda event: (event[0], event[1]))
        output = output_path.with_name(
            f"{output_path.stem}_arrangement_{arr_id}{output_path.suffix or '.mid'}"
        )
        write_midi_events(events, scan.ppq, output)
    return len(scan.arrangement_order)


def write_midi_chunks(
    merged_notes_path: Path,
    merged_note_count: int,
    ppq: int,
    output_path: Path,
    notes_per_file: int,
    cancel: threading.Event | None,
) -> int:
    if notes_per_file <= 0:
        raise ValueError("Notes per file must be greater than zero.")

    chunks: list[list[tuple[int, int, int, int]]] = []
    current: list[tuple[int, int, int, int]] = []
    with merged_notes_path.open("rb", buffering=8 * 1024 * 1024) as notes:
        for index in range(merged_note_count):
            if index % 100_000 == 0:
                check_cancel(cancel)
            raw = read_exact(notes, NOTE_SIZE)
            position = struct.unpack_from("<I", raw, 0)[0]
            length = struct.unpack_from("<I", raw, 8)[0]
            pitch = max(0, min(127, struct.unpack_from("<H", raw, NOTE_KEY_OFFSET)[0]))
            velocity = max(1, min(127, raw[NOTE_VELOCITY_OFFSET]))
            current.append((position, 1, pitch, velocity))
            current.append((position + max(1, length), 0, pitch, 0))
            if (index + 1) % notes_per_file == 0:
                chunks.append(current)
                current = []
    if current:
        chunks.append(current)

    for index, events in enumerate(chunks, 1):
        check_cancel(cancel)
        events.sort(key=lambda event: (event[0], event[1]))
        output = output_path.with_name(
            f"{output_path.stem}_part_{index:03d}{output_path.suffix or '.mid'}"
        )
        write_midi_events(events, ppq, output)
    return len(chunks)


def export_midi(
    source_path: Path,
    output_path: Path,
    options: MergeOptions | None = None,
    status: StatusCallback | None = None,
    cancel: threading.Event | None = None,
    split_by_arrangement: bool = False,
    notes_per_file: int | None = None,
) -> int:
    options = options or MergeOptions()
    status = status or (lambda _message: None)
    source_path = source_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if source_path == output_path:
        raise ValueError("Choose a different output path; the source is never overwritten.")
    if source_path.suffix.lower() != ".flp":
        raise ValueError("The input must be an .flp file.")
    if not source_path.is_file():
        raise FileNotFoundError(f"Input project not found: {source_path}")

    scan = scan_flp(source_path, status, cancel)
    with tempfile.TemporaryDirectory(prefix="flp_midi_export_") as temp_name:
        temp_dir = Path(temp_name)
        clip_path = temp_dir / "pattern_clips.bin"
        extract_pattern_clips(scan, clip_path, options, status, cancel)
        merged_path, merged_count = build_merged_notes(
            scan, clip_path, temp_dir, options, status, cancel
        )
        if merged_count == 0:
            raise FLPFormatError("Pattern Clips were found, but no notes fall inside their visible ranges.")
        status(
            "Writing split MIDI files…"
            if split_by_arrangement or notes_per_file is not None
            else "Writing MIDI file…"
        )
        if notes_per_file is not None:
            files = write_midi_chunks(
                merged_path, merged_count, scan.ppq, output_path,
                notes_per_file, cancel,
            )
            status(f"Done: {merged_count:,} notes exported to {files} MIDI files.")
        elif split_by_arrangement:
            files = write_split_midi_files(
                merged_path, merged_count, scan, output_path, cancel
            )
            status(f"Done: {merged_count:,} notes exported to {files} MIDI files.")
        else:
            write_midi_file(merged_path, merged_count, scan.ppq, output_path, cancel)
            status(f"Done: {merged_count:,} notes exported to {output_path.name}.")
    return merged_count


# ---------------------------------- GUI -----------------------------------


def default_output_for(input_text: str, midi: bool = False) -> str:
    if not input_text:
        return ""
    path = Path(input_text)
    if path.suffix.lower() == ".flp":
        if midi:
            return str(path.with_name(path.stem + ".mid"))
        return str(path.with_name(path.stem + "_merged_notes.flp"))
    return str(path) + (".mid" if midi else "_merged_notes.flp")


def format_size(size: int) -> str:
    if size >= 1024**3:
        return f"{size / (1024**3):.2f} GiB"
    if size >= 1024**2:
        return f"{size / (1024**2):.1f} MiB"
    if size >= 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size} B"


def launch_gui() -> None:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError as exc:
        raise SystemExit("Tkinter is required for the graphical interface.") from exc

    class MergerGUI:
        def __init__(self, root: tk.Tk) -> None:
            self.root = root
            self.root.title(f"{APP_NAME} {APP_VERSION}")
            self.root.geometry("800x660")
            self.root.minsize(700, 590)
            try:
                self.root.iconname(APP_NAME)
            except Exception:
                pass

            self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
            self.worker: threading.Thread | None = None
            self.cancel_event = threading.Event()
            self.output_was_auto = True

            self.input_var = tk.StringVar()
            self.output_var = tk.StringVar()
            self.include_muted_var = tk.BooleanVar(value=True)
            self.turbo_var = tk.BooleanVar(value=True)
            self.export_midi_var = tk.BooleanVar(value=False)
            self.status_var = tk.StringVar(value="Ready")
            self.language_var = tk.StringVar(value="English")

            self._configure_style(ttk)
            self._build(tk, ttk, filedialog)
            self.root.after(100, self._poll)

        def t(self, key: str, **values: object) -> str:
            language = LANGUAGES[self.language_var.get()]
            return TRANSLATIONS[language][key].format(**values)

        def _configure_style(self, ttk_module: object) -> None:
            style = ttk.Style()
            for theme in ("vista", "clam"):
                try:
                    style.theme_use(theme)
                    break
                except Exception:
                    pass
            style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
            style.configure("Sub.TLabel", font=("Segoe UI", 10))
            style.configure("TButton", padding=(10, 6))
            style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"), padding=(14, 8))

        def _build(self, tk: object, ttk: object, filedialog: object) -> None:
            self._tk_module = tk
            self._ttk_module = ttk
            self._filedialog_module = filedialog
            if hasattr(self, "outer"):
                self.outer.destroy()
            outer = ttk.Frame(self.root, padding=18)
            self.outer = outer
            outer.pack(fill="both", expand=True)

            language_row = ttk.Frame(outer)
            language_row.pack(fill="x", pady=(0, 8))
            ttk.Label(language_row, text=self.t("language")).pack(side="right", padx=(0, 6))
            language_box = ttk.Combobox(
                language_row, textvariable=self.language_var, values=list(LANGUAGES), state="readonly", width=12
            )
            language_box.pack(side="right")
            language_box.bind("<<ComboboxSelected>>", self._change_language)

            ttk.Label(outer, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
            ttk.Label(
                outer,
                text=self.t("subtitle"),
                style="Sub.TLabel",
                wraplength=730,
            ).pack(anchor="w", pady=(2, 16))

            files = ttk.LabelFrame(outer, text=self.t("files"), padding=12)
            files.pack(fill="x")
            files.columnconfigure(1, weight=1)

            ttk.Label(files, text=self.t("input")).grid(row=0, column=0, sticky="w", padx=(0, 8), pady=5)
            input_entry = ttk.Entry(files, textvariable=self.input_var)
            input_entry.grid(row=0, column=1, sticky="ew", pady=5)
            ttk.Button(files, text=self.t("browse"), command=self._browse_input).grid(
                row=0, column=2, padx=(8, 0), pady=5
            )

            ttk.Label(files, text=self.t("output")).grid(row=1, column=0, sticky="w", padx=(0, 8), pady=5)
            output_entry = ttk.Entry(files, textvariable=self.output_var)
            output_entry.grid(row=1, column=1, sticky="ew", pady=5)
            ttk.Button(files, text=self.t("browse"), command=self._browse_output).grid(
                row=1, column=2, padx=(8, 0), pady=5
            )
            output_entry.bind("<Key>", lambda _event: setattr(self, "output_was_auto", False))

            options_frame = ttk.LabelFrame(outer, text=self.t("behavior"), padding=12)
            options_frame.pack(fill="x", pady=(12, 0))
            ttk.Checkbutton(
                options_frame,
                text=self.t("turbo"),
                variable=self.turbo_var,
            ).pack(anchor="w")
            ttk.Label(
                options_frame,
                text=self.t("turbo_info"),
                wraplength=730,
            ).pack(anchor="w", padx=(22, 0), pady=(2, 7))
            ttk.Checkbutton(
                options_frame,
                text=self.t("muted"),
                variable=self.include_muted_var,
            ).pack(anchor="w")
            ttk.Label(
                options_frame,
                text=self.t("muted_info"),
                wraplength=710,
            ).pack(anchor="w", padx=(22, 0), pady=(2, 7))
            ttk.Label(
                options_frame,
                text=self.t("arrangements"),
                wraplength=710,
            ).pack(anchor="w")
            ttk.Checkbutton(
                options_frame,
                text=self.t("midi_mode"),
                variable=self.export_midi_var,
                command=self._update_output_mode,
            ).pack(anchor="w", pady=(10, 0))
            action = ttk.Frame(outer)
            action.pack(fill="x", pady=(14, 8))
            self.start_button = ttk.Button(
                action, text=self.t("merge"), style="Primary.TButton", command=self._start
            )
            self.start_button.pack(side="left")
            self.cancel_button = ttk.Button(action, text=self.t("cancel"), command=self._cancel, state="disabled")
            self.cancel_button.pack(side="left", padx=(8, 0))
            self.progress = ttk.Progressbar(action, mode="indeterminate")
            self.progress.pack(side="left", fill="x", expand=True, padx=(16, 0))

            ttk.Label(outer, textvariable=self.status_var).pack(anchor="w", pady=(0, 6))
            log_frame = ttk.LabelFrame(outer, text=self.t("activity"), padding=6)
            log_frame.pack(fill="both", expand=True)
            self.log = tk.Text(
                log_frame,
                height=12,
                wrap="word",
                state="disabled",
                font=("Consolas", 9),
                relief="flat",
            )
            scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
            self.log.configure(yscrollcommand=scrollbar.set)
            self.log.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")

            ttk.Label(
                outer,
                text=self.t("warning"),
                foreground="#8a4b08",
                wraplength=730,
            ).pack(anchor="w", pady=(10, 0))

        def _browse_input(self) -> None:
            path = filedialog.askopenfilename(
                title=self.t("choose_project"),
                filetypes=[(self.t("flp_file"), "*.flp"), (self.t("all_files"), "*.*")],
            )
            if path:
                self.input_var.set(path)
                if self.output_was_auto or not self.output_var.get().strip():
                    self.output_var.set(default_output_for(path, self.export_midi_var.get()))
                    self.output_was_auto = True

        def _update_output_mode(self) -> None:
            if self.output_was_auto:
                self.output_var.set(default_output_for(self.input_var.get().strip(), self.export_midi_var.get()))
            self.start_button.configure(
                text=self.t("export") if self.export_midi_var.get() else self.t("merge")
            )

        def _change_language(self, _event: object = None) -> None:
            self._build(self._tk_module, self._ttk_module, self._filedialog_module)

        def _browse_output(self) -> None:
            midi = self.export_midi_var.get()
            initial = self.output_var.get().strip() or default_output_for(self.input_var.get().strip(), midi)
            path = filedialog.asksaveasfilename(
                title=self.t("save_midi") if midi else self.t("save_flp"),
                defaultextension=".mid" if midi else ".flp",
                initialfile=Path(initial).name if initial else ("export.mid" if midi else "merged_notes.flp"),
                initialdir=str(Path(initial).parent) if initial else None,
                filetypes=[(self.t("midi_file"), "*.mid"), (self.t("all_files"), "*.*")] if midi else [
                    (self.t("flp_file"), "*.flp"), (self.t("all_files"), "*.*")
                ],
            )
            if path:
                self.output_var.set(path)
                self.output_was_auto = False

        def _append_log(self, text: str) -> None:
            self.log.configure(state="normal")
            self.log.insert("end", text.rstrip() + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")

        def _set_running(self, running: bool) -> None:
            self.start_button.configure(state="disabled" if running else "normal")
            self.cancel_button.configure(state="normal" if running else "disabled")
            if running:
                self.progress.start(12)
            else:
                self.progress.stop()

        def _start(self) -> None:
            if self.worker and self.worker.is_alive():
                return
            source_text = self.input_var.get().strip()
            output_text = self.output_var.get().strip()
            exporting_midi = self.export_midi_var.get()
            try:
                if not source_text:
                    raise ValueError(self.t("choose_input"))
                if not output_text:
                    raise ValueError(self.t("choose_output"))
                source = Path(source_text)
                output = Path(output_text)
                if source.expanduser().resolve() == output.expanduser().resolve():
                    raise ValueError(self.t("different"))
                if output.exists():
                    if not messagebox.askyesno(
                        APP_NAME, self.t("replace", path=output)
                    ):
                        return
            except Exception as exc:
                messagebox.showerror(APP_NAME, str(exc))
                return

            self.cancel_event.clear()
            self._set_running(True)
            self.status_var.set(self.t("starting"))
            self._append_log("—" * 72)
            self._append_log(f"Input:  {source}")
            self._append_log(f"Output: {output}")
            self._append_log(self.t("mode_midi") if exporting_midi else self.t("mode_flp"))
            self._append_log(
                "Muted clips: " + (self.t("muted_included") if self.include_muted_var.get() else self.t("muted_skipped"))
            )
            self._append_log(
                self.t("engine")
                + (self.t("turbo_engine") if self.turbo_var.get() else self.t("sorted_engine"))
            )

            options = MergeOptions(
                include_muted_clips=self.include_muted_var.get(),
                turbo_mode=self.turbo_var.get(),
            )

            def work() -> None:
                try:
                    if exporting_midi:
                        count = export_midi(
                            source, output, options=options,
                            status=lambda msg: self.messages.put(("status", msg)),
                            cancel=self.cancel_event,
                        )
                        self.messages.put(("midi_done", count))
                    else:
                        stats = merge_flp(
                            source,
                            output,
                            options=options,
                            status=lambda msg: self.messages.put(("status", msg)),
                            cancel=self.cancel_event,
                        )
                        self.messages.put(("done", stats))
                except MergeCancelled as exc:
                    self.messages.put(("cancelled", str(exc)))
                except Exception as exc:
                    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                    self.messages.put(("error", (str(exc), detail)))

            self.worker = threading.Thread(target=work, name="flp-note-merger", daemon=True)
            self.worker.start()

        def _cancel(self) -> None:
            self.cancel_event.set()
            self.cancel_button.configure(state="disabled")
            self.status_var.set(self.t("cancelling"))
            self._append_log(self.t("cancel_requested"))

        def _poll(self) -> None:
            try:
                while True:
                    kind, payload = self.messages.get_nowait()
                    if kind == "status":
                        text = str(payload)
                        self.status_var.set(text)
                        self._append_log(text)
                    elif kind == "done":
                        stats = payload
                        assert isinstance(stats, MergeStats)
                        self._set_running(False)
                        summary = self.t(
                            "flp_summary", count=stats.merged_notes,
                            clips=stats.included_pattern_clips, pattern=stats.target_pattern_id,
                            size=format_size(stats.output_size), time=stats.elapsed_seconds,
                        )
                        self.status_var.set(self.t("completed"))
                        self._append_log(summary.replace("\n", " | "))
                        messagebox.showinfo(APP_NAME, summary + "\n\n" + self.t("open_verify"))
                    elif kind == "midi_done":
                        self._set_running(False)
                        summary = self.t("midi_summary", count=int(payload), output=self.output_var.get())
                        self.status_var.set(self.t("midi_completed"))
                        self._append_log(summary.replace("\n", " | "))
                        messagebox.showinfo(APP_NAME, summary)
                    elif kind == "cancelled":
                        self._set_running(False)
                        self.status_var.set(self.t("cancelled"))
                        self._append_log(str(payload))
                    elif kind == "error":
                        self._set_running(False)
                        message, detail = payload
                        self.status_var.set(self.t("failed"))
                        self._append_log(detail)
                        messagebox.showerror(APP_NAME, str(message))
            except queue.Empty:
                pass
            self.root.after(100, self._poll)

    root = tk.Tk()
    MergerGUI(root)
    root.mainloop()


# ---------------------------------- CLI -----------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Merge all arranged FL Studio Pattern Clip notes into one optimized, notes-only FLP copy. "
            "Run without arguments to open the Windows GUI."
        )
    )
    parser.add_argument("input", nargs="?", type=Path, help="Source .flp")
    parser.add_argument("output", nargs="?", type=Path, help="Destination .flp (must differ)")
    parser.add_argument(
        "--skip-muted",
        action="store_true",
        help="Skip muted Pattern Clips so the audible note arrangement is preserved.",
    )
    parser.add_argument(
        "--sorted",
        action="store_true",
        help="Disable Turbo mode and globally sort note records (slower compatibility mode).",
    )
    parser.add_argument(
        "--run-records",
        type=int,
        default=150_000,
        help="Notes held per external-sort run when --sorted is used (default: 150000).",
    )
    parser.add_argument(
        "--midi",
        action="store_true",
        help="Export the arranged notes as a Standard MIDI file instead of an FLP copy.",
    )
    parser.add_argument(
        "--split-midi",
        action="store_true",
        help="When exporting MIDI, create one file per FL Arrangement.",
    )
    parser.add_argument(
        "--notes-per-file",
        type=int,
        help="Split MIDI into numbered files with this many notes per file.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.input is None and args.output is None:
        launch_gui()
        return 0
    if args.input is None or args.output is None:
        parser.error("both input and output are required in command-line mode")

    try:
        options = MergeOptions(
            include_muted_clips=not args.skip_muted,
            turbo_mode=not args.sorted,
            sort_run_records=args.run_records,
        )
        if args.midi:
            count = export_midi(
                args.input,
                args.output,
                options=options,
                status=lambda message: print(message, flush=True),
                split_by_arrangement=args.split_midi,
                notes_per_file=args.notes_per_file,
            )
            print(f"Success: {count:,} notes exported to {args.output}")
            return 0
        stats = merge_flp(
            args.input,
            args.output,
            options=options,
            status=lambda message: print(message, flush=True),
        )
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(
        f"Success: {stats.merged_notes:,} notes, {format_size(stats.output_size)}, "
        f"{stats.elapsed_seconds:.1f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
