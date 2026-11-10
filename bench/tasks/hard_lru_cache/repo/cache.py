class LRUCache:
    def __init__(self, capacity: int, on_evict=None):
        self.capacity = capacity
        self.on_evict = on_evict

    def get(self, key):
        raise NotImplementedError

    def put(self, key, value):
        raise NotImplementedError

    def peek(self, key):
        raise NotImplementedError