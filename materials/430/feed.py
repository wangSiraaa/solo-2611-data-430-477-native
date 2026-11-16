import json
from pathlib import Path
class Feed:
    def __init__(self, path):
        doc=json.loads(Path(path).read_text());self.snapshots=doc['snapshots'];self.events=doc['events']
    def snapshot(self, name):
        return dict(self.snapshots[name])
    def __iter__(self):
        for position,event in enumerate(self.events):
            yield position,dict(event)
