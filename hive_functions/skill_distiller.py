


from .memory import _cosine, _hash_embedding
import time as _time_mod

GRACE_PERIOD_SECONDS = 1800

def _parse_iso_time(s: str) -> float:
    """Parse ISO timestamp string to epoch seconds. Returns 0 on failure."""
    try:
        return _time_mod.mktime(_time_mod.strptime(s, '%Y-%m-%dT%H:%M:%S'))
    except Exception:
        return 0.0


class SkillDistiller:
    DECAY_PER_CYCLE = 0.08
    MERGE_THRESHOLD = 0.72
    MIN_SCORE_KEEP  = 0.10
    MAX_INSIGHTS    = 400

    def run(self, insights: list[dict], focus_paths: list[str] = []) -> list[dict]:
        if not insights:
            return insights
        updated = self._decay(insights, focus_paths)
        updated = self._merge_similar(updated)
        updated = self._evict(updated)
        return updated

    def _decay(self, insights, focus_paths):
        fps = [p.replace('\\', '/').strip().lower() for p in (focus_paths or [])]
        # PERF-5 FIX: Compute current time ONCE instead of calling
        # strftime+strptime for every insight. strptime is expensive (~5us/call)
        # and with 400 insights = 2ms of pure string parsing per cycle.
        now = _time_mod.time()
        grace_seconds = GRACE_PERIOD_SECONDS
        result = []
        for ins in insights:
            ins = dict(ins)
            score = float(ins.get('relevance_score', 1.0))
            tp = ins.get('trigger_path', '').lower()
            saved_at = _parse_iso_time(ins.get('saved_at', ''))
            age_seconds = max(0, now - saved_at) if now and saved_at else 999999
            in_grace = age_seconds < grace_seconds and score >= 0.5
            if in_grace:
                # Fresh + decent score → no decay, just keep it
                pass
            elif any(tp.startswith(fp) or fp.startswith(tp) for fp in fps if fp):
                score = min(1.0, score + 0.05)
            else:
                score = max(0.0, score - self.DECAY_PER_CYCLE)
            ins['relevance_score'] = round(score, 4)
            result.append(ins)
        return result

    def _merge_similar(self, insights):
        # PERF-1 FIX: Pre-filter by trigger_path overlap before computing
        # expensive cosine similarity. With 400 insights the naive approach
        # does 400*399/2 = ~80k cosine comparisons. Path pre-filtering
        # typically reduces this to ~5-10k.
        used = [False] * len(insights)
        # Pre-extract trigger_paths for cheap comparison
        paths = [ins.get('trigger_path', '').lower() for ins in insights]
        result = []
        for i, ins_a in enumerate(insights):
            if used[i]:
                continue
            cluster = [ins_a]
            emb_a = ins_a.get('embedding') or []
            path_a = paths[i]
            for j, ins_b in enumerate(insights[i+1:], start=i+1):
                if used[j]:
                    continue
                # Cheap path-filter: only compute cosine if paths overlap
                # or if one of them has no path (generic insights match all)
                path_b = paths[j]
                _paths_related = (
                    not path_a or not path_b
                    or path_a.startswith(path_b) or path_b.startswith(path_a)
                    or path_a == path_b
                )
                if not _paths_related:
                    continue
                emb_b = ins_b.get('embedding') or []
                if emb_a and emb_b and _cosine(emb_a, emb_b) >= self.MERGE_THRESHOLD:
                    cluster.append(ins_b)
                    used[j] = True
            used[i] = True
            if len(cluster) == 1:
                result.append(ins_a)
            else:
                result.append(self._merge_cluster(cluster))
        return result

    def _merge_cluster(self, cluster):
        base = max(cluster, key=lambda x: len(x.get('insight', '')))
        merged = dict(base)
        merged['merge_count'] = sum(c.get('merge_count', 1) for c in cluster)
        # Use the highest relevance_score in the cluster (not the base's score).
        # The base is picked by longest text, which may have a low score.
        # Merging similar insights should boost confidence, so we take max + bonus.
        _best_score = max(float(c.get('relevance_score', 0.5)) for c in cluster)
        merged['relevance_score'] = min(1.0, _best_score + 0.1 * (len(cluster) - 1))
        all_paths = list({c.get('trigger_path', '') for c in cluster if c.get('trigger_path')})
        merged['trigger_paths'] = all_paths[:5]
        # Recompute embedding from the current insight text — the old embedding
        # may no longer be representative after merging similar insights.
        merged['embedding'] = _hash_embedding(merged.get('insight', ''))
        return merged

    def _evict(self, insights):
        alive = [i for i in insights if float(i.get('relevance_score', 1.0)) >= self.MIN_SCORE_KEEP]
        alive.sort(key=lambda x: float(x.get('relevance_score', 0)), reverse=True)
        return alive[:self.MAX_INSIGHTS]
