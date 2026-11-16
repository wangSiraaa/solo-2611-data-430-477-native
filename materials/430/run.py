import sys,json
from feed import Feed
from book import Book
from publisher import Publisher

def run(path):
    feed=Feed(path);books={};publisher=Publisher()
    for pos,event in feed:
        symbol=event['symbol'];book=books.setdefault(symbol,Book())
        if event['kind']=='snapshot':book.snapshot(feed.snapshot(event['name']))
        elif event['kind']=='delta':book.delta(event)
        elif event['kind']=='observe':publisher.publish(pos,symbol,book.view())
        else:raise ValueError('unknown event')
    return publisher.observations()
if __name__=='__main__':print(json.dumps(run(sys.argv[1]),ensure_ascii=False,sort_keys=True))
