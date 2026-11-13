"""Разметка исходного текста по реальной речи, с явной оценкой уверенности."""
import os
import re
from difflib import SequenceMatcher
from pathlib import Path

from studio.agents import Unavailable


class AlignmentMismatch(ValueError):
    """Речь не совпадает с текстом; ожидание провайдера проблему не исправит."""


def normalize(word):
    return "".join(re.findall(r"[a-zа-я0-9]+", word.casefold().replace("ё", "е")))


class Captioner:
    def __init__(self, model=None):
        self.model = model
        if model is None and os.getenv("STUDIO_ALIGNER", "estimated") == "whisper":
            try:
                from faster_whisper import WhisperModel
                root = Path(os.getenv("STUDIO_DATA", "data"))/"models"
                self.model = WhisperModel("tiny", device="cpu", compute_type="int8", cpu_threads=2, num_workers=1, download_root=str(root))
            except (ImportError, OSError, RuntimeError):
                if os.getenv("STUDIO_ALIGNMENT_REQUIRED") == "true":
                    raise Unavailable("Не доступна серверная ASR-модель для субтитров") from None

    def align(self, audio, text, duration):
        words = text.split()
        if not words:
            raise ValueError("Пустая реплика не допускается")
        estimated = [{"text": w, "start": duration*i/len(words), "end": duration*(i+1)/len(words)} for i,w in enumerate(words)]
        result = {"method": "estimated", "confidence": None, "words": estimated}
        if self.model is None:
            return result
        try:
            segments, _ = self.model.transcribe(str(audio), language="ru", beam_size=1, word_timestamps=True, vad_filter=False, condition_on_previous_text=False)
            recognized = [w for segment in segments for w in (segment.words or []) if normalize(w.word)]
            matcher = SequenceMatcher(None, [normalize(w) for w in words], [normalize(w.word) for w in recognized], autojunk=False)
            anchors = {}
            for block in matcher.get_matching_blocks():
                for offset in range(block.size):
                    w = recognized[block.b+offset]
                    anchors[block.a+offset] = max(0., min(duration, (float(w.start)+float(w.end))/2))
            confidence = len(anchors)/len(words)
            if confidence < .7:
                if os.getenv("STUDIO_ALIGNMENT_REQUIRED") == "true":
                    raise AlignmentMismatch("Речь плохо совпала со сценарием; нужна правка озвучки")
                return result | {"confidence": confidence}
            centers = []
            for i in range(len(words)):
                if i in anchors:
                    centers.append(anchors[i])
                else:
                    left = max((k for k in anchors if k < i), default=-1)
                    right = min((k for k in anchors if k > i), default=len(words))
                    a, b = anchors.get(left, 0.), anchors.get(right, duration)
                    centers.append(a+(b-a)*(i-left)/(right-left))
            edges = [0.] + [(centers[i-1]+centers[i])/2 for i in range(1,len(words))] + [duration]
            if any(a >= b for a,b in zip(edges, edges[1:])):
                return result | {"confidence": confidence}
            return {"method": "whisper_assisted", "confidence": confidence, "words": [{"text": w, "start": edges[i], "end": edges[i+1]} for i,w in enumerate(words)]}
        except AlignmentMismatch:
            raise
        except (ValueError, RuntimeError, OSError):
            if os.getenv("STUDIO_ALIGNMENT_REQUIRED") == "true":
                raise Unavailable("Не удалось разметить речь по времени") from None
            return result


def groups(words):
    group = []
    for word in words:
        if group and (len(group) >= 6 or len(" ".join(w["text"] for w in group)+" "+word["text"]) > 45):
            yield group
            group = []
        group.append(word)
    if group:
        yield group
