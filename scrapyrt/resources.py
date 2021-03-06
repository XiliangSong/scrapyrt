# -*- coding: utf-8 -*-
from scrapy.utils.misc import load_object
from scrapy.utils.serialize import ScrapyJSONEncoder
from twisted.internet.defer import Deferred
from twisted.web import server, resource
from twisted.web.error import UnsupportedMethod, Error
import demjson

from . import log
from .core import CrawlManager
from .conf import settings


class ServiceResource(resource.Resource, object):
    """Taken from scrapyd and changed."""
    json_encoder = ScrapyJSONEncoder()

    def __init__(self, root=None):
        resource.Resource.__init__(self)
        self.root = root

    def render(self, request):
        try:
            result = resource.Resource.render(self, request)
        except Exception as e:
            result = self.handle_render_errors(request, e)

        if not isinstance(result, Deferred):
            return self.render_object(result, request)

        # deferred result - add appropriate callbacks and errbacks
        def handle_errors(failure):
            return self.handle_render_errors(request, failure.value)

        def finish_request(obj):
            request.write(self.render_object(obj, request))
            request.finish()

        result.addErrback(handle_errors)
        result.addCallback(finish_request)
        return server.NOT_DONE_YET

    def handle_render_errors(self, request, exception):
        """Override this method to add custom exception handling."""
        if request.code == 200:
            # Default code - means that error wasn't handled
            if isinstance(exception, UnsupportedMethod):
                request.setResponseCode(405)
            elif isinstance(exception, Error):
                code = int(exception.status)
                request.setResponseCode(code)
                if code == 500:
                    log.err()
            else:
                request.setResponseCode(500)
                log.err()
        return self.format_error_response(request, exception)

    def format_error_response(self, request, exception):
        return {
            "status": "error",
            "message": str(exception.message),
            "code": request.code
        }

    def render_object(self, obj, request):
        r = self.json_encoder.encode(obj) + "\n"
        request.setHeader('Content-Type', 'application/json')
        request.setHeader('Access-Control-Allow-Origin', '*')
        request.setHeader('Access-Control-Allow-Methods',
                          ', '.join(getattr(self, 'allowedMethods', [])))
        request.setHeader('Access-Control-Allow-Headers', 'X-Requested-With')
        request.setHeader('Content-Length', len(r))
        return r


class RealtimeApi(ServiceResource):

    def __init__(self, **kwargs):
        super(RealtimeApi, self).__init__(self)
        for route, resource_path in settings.RESOURCES.iteritems():
            resource_cls = load_object(resource_path)
            self.putChild(route, resource_cls(self, **kwargs))


class CrawlResource(ServiceResource):

    isLeaf = True
    allowedMethods = ['GET', 'POST']

    def render_GET(self, request, **kwargs):
        """Request querysting must contain following keys: url, spider_name.

        At the moment kwargs for scrapy request are not supported in GET.
        They are supported in POST handler.
        """
        request_data = dict((name, value[0]) for name, value
                            in request.args.items())

        spider_data = {
            'url': self.get_required_argument(request_data, 'url'),
            # TODO get optional Request arguments here
            # distinguish between proper Request args and
            # api parameters
        }
        try:
            callback = request_data['callback']
        except KeyError:
            pass
        else:
            spider_data['callback'] = callback
        return self.prepare_crawl(request_data, spider_data, **kwargs)

    def render_POST(self, request, **kwargs):
        """
        :param request:
            body should contain JSON

        Required keys in JSON posted:

        :spider_name: string
            name of spider to be scheduled.

        :request: json object
            request to be scheduled with spider.
            Note: request must contain url for spider.
            It may contain kwargs to scrapy request.

        """
        request_body = request.content.getvalue()
        try:
            request_data = demjson.decode(request_body)
        except ValueError as e:
            message = "Invalid JSON in POST body. {}"
            message.format(e.pretty_description())
            raise Error('400', message=message)

        log.msg("{}".format(request_data))
        spider_data = self.get_required_argument(request_data, "request")
        error_msg = "Missing required key 'url' in 'request' object"
        self.get_required_argument(spider_data, "url", error_msg=error_msg)

        return self.prepare_crawl(request_data, spider_data, **kwargs)

    def get_required_argument(self, request_data, name, error_msg=None):
        """Get required API key from dict-like object.

        :param dict request_data:
            dictionary with names and values of parameters supplied to API.
        :param str name:
            required key that must be found in request_data
        :return: value of required param
        :raises Error: Bad Request response

        """
        if error_msg is None:
            error_msg = 'Missing required parameter: {}'.format(repr(name))
        try:
            value = request_data[name]
        except KeyError:
            raise Error('400', message=error_msg)
        if not value:
            raise Error('400', message=error_msg)
        return value

    def prepare_crawl(self, request_data, spider_data, *args, **kwargs):
        """Schedule given spider with CrawlManager.

        :param dict request_data:
            arguments needed to find spider and set proper api parameters
            for crawl (max_requests for example)

        :param dict spider_data:
            should contain positional and keyword arguments for Scrapy
            Request object that will be created
        """
        spider_name = self.get_required_argument(request_data, 'spider_name')
        try:
            max_requests = request_data['max_requests']
        except (KeyError, IndexError):
            max_requests = None
        dfd = self.run_crawl(
            spider_name, spider_data, max_requests, *args, **kwargs)
        dfd.addCallback(
            self.prepare_response, request_data=request_data, *args, **kwargs)
        return dfd

    def run_crawl(self, spider_name, spider_data,
                  max_requests=None, *args, **kwargs):
        manager = CrawlManager(spider_name, spider_data, max_requests)
        dfd = manager.crawl(*args, **kwargs)
        return dfd

    def prepare_response(self, result, *args, **kwargs):
        items = result.get("items")
        response = {
            "status": "ok",
            "items": items,
            "items_dropped": result.get("items_dropped", []),
            "stats": result.get("stats"),
            "spider_name": result.get("spider_name"),
        }
        errors = result.get("errors")
        if errors:
            response["errors"] = errors
        return response

