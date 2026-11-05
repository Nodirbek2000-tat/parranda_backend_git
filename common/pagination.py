from rest_framework.pagination import PageNumberPagination


class StandardPagination(PageNumberPagination):
    """``?page=N`` and ``?page_size=`` (max 200), as required by the API contract."""

    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200
