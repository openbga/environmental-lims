from django.urls import path

from . import portal_views as views
from .edd import views as edd

app_name = "portal"
urlpatterns = [
    path("", views.home, name="home"),
    path("projects/", views.project_list, name="project-list"),
    path("samples/", views.sample_list, name="sample-list"),
    path("reports/", views.report_list, name="report-list"),
    path(
        "reports/csv-template-example.csv",
        views.result_template_example,
        name="csv-template-example",
    ),
    path(
        "reports/<int:pk>/v<int:number>/csv-template/",
        views.report_csv_template,
        name="report-csv-template",
    ),
    path("reports/<int:pk>/", views.report_detail, name="report-detail"),
    path(
        "reports/<int:pk>/v<int:number>/samples/<int:position>/location-history/",
        views.location_history,
        name="location-history",
    ),
    path("reports/<int:pk>/v<int:number>.pdf", views.report_pdf, name="report-pdf"),
    path(
        "reports/<int:pk>/v<int:number>/results.csv",
        views.report_results_download,
        {"output_format": "csv"},
        name="report-results-csv",
    ),
    path(
        "reports/<int:pk>/v<int:number>/results.xlsx",
        views.report_results_download,
        {"output_format": "xlsx"},
        name="report-results-xlsx",
    ),
    path("reports/<int:pk>/edd/<int:export_pk>.zip", edd.portal_edd_download, name="edd-download"),
]
