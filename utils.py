import logging
import re

def natural_keys(text):
    if text is None: text = ""
    text = str(text)
    text = text.replace('幼儿园', '0幼儿园').replace('小学', '1小学').replace('初中', '2初中').replace('高中', '3高中').replace('大学', '4大学')
    cn_map = {'零': '0', '一': '1', '二': '2', '三': '3', '四': '4', '五': '5', '六': '6', '七': '7', '八': '8', '九': '9', '十': '10'}
    for k, v in cn_map.items(): text = text.replace(k, v)
    return [int(c) if c.isdigit() else c for c in re.split(r'(\d+)', text)]

def is_complex_password(pwd):
    if pwd == '123456': return True
    has_letter = any(c.isalpha() for c in pwd)
    has_digit = any(c.isdigit() for c in pwd)
    return len(pwd) >= 6 and has_letter and has_digit

class QueueHandler(logging.Handler):
    def __init__(self, log_queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        msg = self.format(record)
        self.log_queue.put(msg)