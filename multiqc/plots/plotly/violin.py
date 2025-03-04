import copy
import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Set, Tuple, Union

import numpy as np
import plotly.graph_objects as go  # type: ignore

from multiqc import config, report
from multiqc.plots.plotly.plot import BaseDataset, PConfig, Plot, PlotType
from multiqc.plots.plotly.table import make_table
from multiqc.plots.table_object import ColumnAnchor, ColumnMeta, DataTable, TableConfig, ValueT
from multiqc.types import Anchor, SampleName, Section

logger = logging.getLogger(__name__)


def plot(dts: List[DataTable], show_table_by_default: bool = False) -> "ViolinPlot":
    """
    Build and add the plot data to the report, return an HTML wrapper.
    """
    return ViolinPlot.create(dts, show_table_by_default)


@dataclass
class XAxis:
    ticksuffix: Optional[str] = None
    tickvals: Optional[List[int]] = None
    range: Optional[List[Union[float, int]]] = None


@dataclass
class ViolinColumn:
    title: str
    description: str
    suffix: str
    dmin: Optional[Union[float, int]]
    dmax: Optional[Union[float, int]]
    hidden: bool
    xaxis: XAxis
    show_only_outliers: Optional[bool]
    show_points: Optional[bool]
    namespace: Optional[str] = None
    color: Optional[str] = None
    hoverformat: Optional[str] = None

    def model_dump(self) -> Dict[str, Any]:
        d = self.__dict__
        d["xaxis"] = self.xaxis.__dict__
        return d


VIOLIN_HEIGHT = 70  # single violin height
EXTRA_HEIGHT = 63  # extra space for the title and footer


class Dataset(BaseDataset):
    metrics: List[ColumnAnchor]
    header_by_metric: Dict[ColumnAnchor, ViolinColumn]
    violin_value_by_sample_by_metric: Dict[ColumnAnchor, Dict[SampleName, Union[int, float, str, None]]]
    scatter_value_by_sample_by_metric: Dict[ColumnAnchor, Dict[SampleName, Union[int, float, str, None]]]
    all_samples: List[SampleName]  # unique list of all samples in this dataset
    scatter_trace_params: Dict[str, Any]
    dt: DataTable
    show_table_by_default: bool
    is_downsampled: bool

    @staticmethod
    def values_and_headers_from_dt(
        dt: DataTable,
    ) -> Tuple[
        Dict[ColumnAnchor, Dict[SampleName, ValueT]],
        Dict[ColumnAnchor, ColumnMeta],
    ]:
        value_by_sample_by_metric: Dict[ColumnAnchor, Dict[SampleName, ValueT]] = {}
        dt_column_by_metric: Dict[ColumnAnchor, ColumnMeta] = {}

        for idx, metric_name, dt_column in dt.get_headers_in_order():
            value_by_sample: Dict[SampleName, ValueT] = {}
            for _, group_rows in dt.sections[idx].rows_by_sgroup.items():
                for row in group_rows:
                    try:
                        v = row.raw_data[metric_name]
                    except KeyError:
                        pass
                    else:
                        assert v is not None and str(v).strip != "", v
                        value_by_sample[row.sample] = v

            value_by_sample_by_metric[dt_column.rid] = value_by_sample

        for idx, metric_name, dt_column in dt.get_headers_in_order():
            dt_column_by_metric[dt_column.rid] = dt_column

        # If all colors are the same, remove them
        if len(set([t_col.color for t_col in dt_column_by_metric.values()])) == 1:
            for t_col in dt_column_by_metric.values():
                t_col.color = None

        return value_by_sample_by_metric, dt_column_by_metric

    @staticmethod
    def create(
        dataset: BaseDataset,
        dt: DataTable,
        show_table_by_default: bool,
    ) -> "Dataset":
        value_by_sample_by_metric, dt_column_by_metric = Dataset.values_and_headers_from_dt(dt)

        all_samples: Set[SampleName] = set()
        scatter_value_by_sample_by_metric: Dict[ColumnAnchor, Dict[SampleName, Union[int, float, str, None]]] = {}
        violin_value_by_sample_by_metric = {}
        header_by_metric: Dict[ColumnAnchor, ViolinColumn] = {}
        metrics: List[ColumnAnchor] = []

        for col_anchor, dt_column in dt_column_by_metric.items():
            column = ViolinColumn(
                namespace=dt_column.namespace,
                title=dt_column.title,
                description=dt_column.description,
                suffix=dt_column.suffix or "",
                dmax=dt_column.dmax,
                dmin=dt_column.dmin,
                hidden=dt_column.hidden,
                color=dt_column.color,
                xaxis=XAxis(ticksuffix=dt_column.suffix),
                show_points=True,
                show_only_outliers=False,
            )
            header_by_metric[col_anchor] = column

            value_by_sample = value_by_sample_by_metric[col_anchor]
            if not value_by_sample:
                logger.debug(f"No non-empty values found for metric: {column.title}")
                continue

            values_are_numeric = all(isinstance(v, (int, float)) for v in value_by_sample.values())
            values_are_integer = all(isinstance(v, int) for v in value_by_sample.values())
            if values_are_numeric:
                # Remove NaN and Inf values
                value_by_sample = {s: v for s, v in value_by_sample.items() if np.isfinite(v)}
                if not value_by_sample:
                    logger.warning(f"All values are NaN or Inf for metric: {column.title}")
                    continue

            column.show_points = len(value_by_sample) <= config.violin_min_threshold_no_points
            column.show_only_outliers = len(value_by_sample) > config.violin_min_threshold_outliers

            if values_are_numeric:
                # Calculate range
                xmin = column.dmin
                xmax = column.dmax
                tickvals = None
                if xmin == xmax == 0:  # Plotly will modify the 0:0 range to -1:1, and we want to keep it 0-centered
                    xmax = 1
                    tickvals = [0]
                column.xaxis.tickvals = tickvals
                if xmin is not None and xmax is not None:
                    if column.show_points:
                        # add extra padding to avoid clipping the points at range limits
                        xmin -= (xmax - xmin) * 0.005
                        xmax += (xmax - xmin) * 0.005
                    column.xaxis.range = [xmin, xmax]

            if not column.show_points:  # Do not add any interactive points
                scatter_value_by_sample: Dict[SampleName, Union[int, float, str, None]] = {}
            elif not column.show_only_outliers:
                scatter_value_by_sample = {}  # will use the violin values
            else:
                # Sample number is > header['show_only_outliers']
                if not values_are_numeric:
                    # As values are not numeric, will not add any interactive points
                    scatter_value_by_sample = {}
                else:
                    # For numbers, finding outliers and adding only them as interactive points
                    samples = list(value_by_sample.keys())
                    numeric_values: List[Union[int, float]] = []
                    for v in value_by_sample.values():
                        assert isinstance(v, (int, float))  # values_are_numeric assures that all values are numeric
                        numeric_values.append(v)
                    outlier_statuses = find_outliers(
                        numeric_values,
                        minval=column.dmin,
                        maxval=column.dmax,
                        metric=column.title,
                    )
                    scatter_value_by_sample = {
                        samples[idx]: numeric_values[idx] for idx in range(len(samples)) if outlier_statuses[idx]
                    }

            scatter_value_by_sample_by_metric[col_anchor] = scatter_value_by_sample

            # Now sort and downsample values to keep max 2000 points for each metric
            violin_value_by_sample = value_by_sample
            max_violin_points = config.violin_downsample_after
            if max_violin_points is not None and len(violin_value_by_sample) > max_violin_points:
                logger.debug(
                    f"Violin for '{column.title}': sample number is {len(violin_value_by_sample)}. "
                    f"Will downsample to max {max_violin_points} points."
                )
                samples = list(violin_value_by_sample.keys())
                values = list(violin_value_by_sample.values())
                indices = np.argsort(values)
                indices = indices[:: int(math.ceil(len(indices) / max_violin_points))]
                violin_value_by_sample = {samples[idx]: values[idx] for idx in indices}

            violin_value_by_sample_by_metric[col_anchor] = violin_value_by_sample

            # Clean up the header
            if values_are_numeric and not values_are_integer:
                tt_decimals = dt_column.tt_decimals if dt_column.tt_decimals is not None else 2
                column.hoverformat = f".{tt_decimals}f"

            all_samples.update(set(list(scatter_value_by_sample.keys())))
            all_samples.update(set(list(violin_value_by_sample.keys())))
            metrics.append(col_anchor)

        is_downsampled = (
            config.violin_downsample_after is not None and len(all_samples) > config.violin_downsample_after
        )

        ds = Dataset(
            **dataset.model_dump(),
            metrics=metrics,
            header_by_metric=header_by_metric,
            violin_value_by_sample_by_metric=violin_value_by_sample_by_metric,
            scatter_value_by_sample_by_metric=scatter_value_by_sample_by_metric,
            all_samples=sorted(all_samples),
            scatter_trace_params=dict(),
            dt=dt,
            show_table_by_default=show_table_by_default,
            is_downsampled=is_downsampled,
        )

        ds.trace_params.update(
            orientation="h",
            box={"visible": True},
            meanline={"visible": True},
            fillcolor="#b5b5b5",
            line={"width": 2, "color": "#b5b5b5"},
            opacity=0.5,
            points=False,  # Don't show points, we'll add them manually
            # The hover information is useful, but the formatting is ugly and not
            # configurable as far as I can see. Also, it's not possible to disable it,
            # so setting it to "points" as we don't show points, so it's effectively
            # disabling it.
            hoveron="points",
        )

        # If all violins are grey, make the dots blue to make it more clear that it's interactive
        # if some violins are color-coded, make the dots black to make them less distracting
        marker_color = "black" if any(h.color is not None for h in ds.header_by_metric.values()) else "#0b79e6"
        ds.scatter_trace_params = {
            "mode": "markers",
            "marker": {
                "size": 4,
                "color": marker_color,
            },
            "showlegend": False,
            "hovertemplate": ds.trace_params["hovertemplate"],
            "hoverlabel": {"bgcolor": "white"},
        }
        return ds

    def create_figure(
        self,
        layout: go.Layout,
        is_log: bool = False,
        is_pct: bool = False,
        add_scatter: bool = True,
        **kwargs,
    ) -> go.Figure:
        """
        Create a Plotly figure for a dataset
        """
        metrics = [m for m in self.metrics if not self.header_by_metric[m].hidden]
        if len(metrics) == 0:
            return go.Figure(layout=layout)

        layout = copy.deepcopy(layout)
        layout.grid.rows = len(metrics)
        layout.grid.subplots = [[(f"x{i + 1}y{i + 1}" if i > 0 else "xy")] for i in range(len(metrics))]
        layout.height = VIOLIN_HEIGHT * len(metrics) + EXTRA_HEIGHT

        for metric_idx, metric in enumerate(metrics):
            header = self.header_by_metric[metric]

            layout[f"xaxis{metric_idx + 1}"] = {
                "automargin": layout["xaxis"]["automargin"],
                "color": layout["xaxis"]["color"],
                "gridcolor": layout["xaxis"]["gridcolor"],
                "zerolinecolor": layout["xaxis"]["zerolinecolor"],
                "hoverformat": layout["xaxis"]["hoverformat"],
                "tickfont": copy.deepcopy(layout["xaxis"]["tickfont"]),
            }
            layout[f"xaxis{metric_idx + 1}"].update(header.xaxis.__dict__)
            layout[f"yaxis{metric_idx + 1}"] = {
                "automargin": layout["yaxis"]["automargin"],
                "color": layout["yaxis"]["color"],
                "gridcolor": layout["yaxis"]["gridcolor"],
                "zerolinecolor": layout["yaxis"]["zerolinecolor"],
                "hoverformat": layout["yaxis"]["hoverformat"],
                "tickfont": copy.deepcopy(layout["yaxis"]["tickfont"]),
            }

            padding = "  "  # otherwise the labels will stick too close to the axis
            title = header.title + padding
            if header.namespace:
                title = f"{header.namespace}{padding}<br>" + title
            layout[f"yaxis{metric_idx + 1}"].update(
                {
                    "tickmode": "array",
                    "tickvals": [metric_idx],
                    "ticktext": [title],
                }
            )

            if header.hoverformat:
                layout[f"xaxis{metric_idx + 1}"]["hoverformat"] = header.hoverformat

            if header.color:
                layout[f"yaxis{metric_idx + 1}"]["tickfont"] = {
                    "color": f"rgb({header.color})",
                }

        layout["xaxis"] = layout["xaxis1"]
        layout["yaxis"] = layout["yaxis1"]

        fig = go.Figure(layout=layout)

        violin_values_by_sample_by_metric = self.violin_value_by_sample_by_metric

        for metric_idx, metric in enumerate(metrics):
            header = self.header_by_metric[metric]
            params = copy.deepcopy(self.trace_params)
            if header.color:
                params["fillcolor"] = f"rgb({header.color})"
                params["line"]["color"] = f"rgb({header.color})"

            violin_values_by_sample = violin_values_by_sample_by_metric[metric]
            axis_key = "" if metric_idx == 0 else str(metric_idx + 1)
            fig.add_trace(
                go.Violin(
                    x=list(violin_values_by_sample.values()),
                    name=metric_idx,
                    text=list(violin_values_by_sample.keys()),
                    xaxis=f"x{axis_key}",
                    yaxis=f"y{axis_key}",
                    **params,
                ),
            )

            if add_scatter and header.show_points:
                if header.show_only_outliers:
                    scatter_values_by_sample = self.scatter_value_by_sample_by_metric[metric]
                else:
                    scatter_values_by_sample = violin_values_by_sample
                scatter_params = copy.deepcopy(self.scatter_trace_params)
                for sample, value in scatter_values_by_sample.items():
                    # add vertical jitter (not working in python version currently)
                    y = float(metric_idx)
                    # y += random.uniform(-0.2, 0.2)
                    # y += random.random() * 0.3 - 0.3 / 2
                    fig.add_trace(
                        go.Scatter(
                            x=[value],
                            y=[y],
                            text=[sample],
                            xaxis=f"x{axis_key}",
                            yaxis=f"y{axis_key}",
                            **scatter_params,
                        ),
                    )
        return fig

    def save_data_file(self) -> None:
        data: Dict[str, Dict[str, Union[int, float, str, None]]] = {}
        for metric in self.metrics:
            values_by_sample = self.violin_value_by_sample_by_metric[metric]
            title = self.header_by_metric[metric].title
            for sample, value in values_by_sample.items():
                data.setdefault(sample, {})[title] = value

        report.write_data_file(data, self.uid)

    def format_dataset_for_ai_prompt(self, pconfig: TableConfig, keep_hidden: bool = True) -> str:
        """Format as a markdown table"""
        headers = self.dt.get_headers_in_order(keep_hidden=keep_hidden)
        samples = self.all_samples

        result = "Number of samples: " + str(len(samples)) + "\n"
        if self.is_downsampled:
            result += (
                f"Note: sample number {len(samples)} is greater than the threshold {config.violin_downsample_after}, "
                "so data points were downsampled to fit the context window. However, outliers for each metric were "
                "identified and kept in the datasets.\n"
            )
        result += "\n"

        # List metrics: metric name to description
        result += "Metrics:\n" + "\n".join(f"{col.title} - {col.description}" for _, _, col in headers)
        result += "\n\n"

        # Table view - rows are samples, columns are metrics
        result += "|" + pconfig.col1_header + "|" + "|".join(col.title for (_, _, col) in headers) + "|\n"
        result += "|---|" + "|".join("---" for _ in headers) + "|\n"
        for sample in samples:
            if all(
                sample not in self.violin_value_by_sample_by_metric[col.rid]
                and sample not in self.scatter_value_by_sample_by_metric[col.rid]
                for _, _, col in headers
            ):
                continue
            pseudonym = report.anonymize_sample_name(sample)
            row = []
            for _, _, col in headers:
                value = self.violin_value_by_sample_by_metric.get(col.rid, {}).get(
                    sample, self.scatter_value_by_sample_by_metric.get(col.rid, {}).get(sample, "")
                )
                if value != "":
                    if col.suffix:
                        value = f"{value}{col.suffix}"
                row.append(str(value))
            result += f"|{pseudonym}|" + "|".join(row) + "|\n"

        return result


class ViolinPlot(Plot[Dataset, TableConfig]):
    datasets: List[Dataset]
    violin_height: int = VIOLIN_HEIGHT  # single violin height
    extra_height: int = EXTRA_HEIGHT  # extra space for the title and footer
    no_violin: bool
    show_table: bool
    show_table_by_default: bool
    n_samples: int
    table_anchor: Anchor

    def samples_names(self) -> List[SampleName]:
        names: List[SampleName] = []
        for ds in self.datasets:
            names.extend(SampleName(s) for s in ds.all_samples)
        return names

    @staticmethod
    def create(
        dts: List[DataTable],
        show_table_by_default: bool = False,
    ) -> "ViolinPlot":
        assert len(dts) > 0, "No datasets to plot"

        samples_per_dataset: List[Set[str]] = []
        for _, dt in enumerate(dts):
            ds_samples: Set[str] = set()
            for section in dt.sections:
                ds_samples.update(section.rows_by_sgroup.keys())
            samples_per_dataset.append(ds_samples)

        model: Plot[Dataset, TableConfig] = Plot.initialize(
            plot_type=PlotType.VIOLIN,
            pconfig=dts[0].pconfig,
            n_samples_per_dataset=[len(x) for x in samples_per_dataset],
            id=dts[0].id,
            default_tt_label=": %{x}",
            # Violins scale well, so can always keep them interactive and visible:
            defer_render_if_large=False,
            flat_if_very_large=False,
        )

        no_violin: bool = model.pconfig.no_violin
        show_table_by_default = show_table_by_default or no_violin

        model.datasets = [
            Dataset.create(ds, dt, show_table_by_default)
            for ds, dt in zip(
                model.datasets,
                dts,
            )
        ]

        # Violin-specific layout parameters
        model.layout.update(
            margin=dict(
                pad=0,
                b=40,
            ),
            xaxis=dict(
                # so Plotly doesn't try to fit the ticks the on the most bottom violin,
                # and squish the other violins
                automargin=False,
                tickfont=dict(size=9, color="rgba(0,0,0,0.5)"),
                gridcolor="rgba(0,0,0,0.1)",
                zerolinecolor="rgba(0,0,0,0.1)",
            ),
            yaxis=dict(
                tickfont=dict(size=9, color="rgba(0,0,0,0.5)"),
                gridcolor="rgba(0,0,0,0.1)",
                zerolinecolor="rgba(0,0,0,0.1)",
            ),
            violingap=0,
            grid=dict(
                columns=1,
                roworder="top to bottom",
                ygap=0.4,
            ),
            showlegend=False,
        )

        # If the number of samples is high:
        # - do not add a table
        # - plot a Violin in Python, and serialise the figure instead of the datasets
        show_table = True
        max_n_samples = max(len(x) for x in samples_per_dataset)
        if max_n_samples > config.max_table_rows and not no_violin:
            show_table = False
            if show_table_by_default:
                logger.debug(
                    f"Table '{model.anchor}': sample number {max_n_samples} > {config.max_table_rows}, "
                    "Will render only a violin plot instead of the table"
                )

        return ViolinPlot(
            **model.__dict__,
            no_violin=no_violin,
            show_table=show_table,
            show_table_by_default=show_table_by_default,
            n_samples=max_n_samples,
            table_anchor=model.datasets[0].dt.anchor,
        )

    def buttons(self, flat: bool, module_anchor: Anchor, section_anchor: Anchor) -> List[str]:
        """Add a control panel to the plot"""
        buttons: List[str] = []
        if not flat and any(len(ds.metrics) > 1 for ds in self.datasets):
            buttons.append(
                self._btn(
                    cls="mqc_table_config_modal_btn",
                    label="<span class='glyphicon glyphicon-th'></span> Configure columns",
                    data_attrs={"toggle": "modal", "target": f"#{self.datasets[0].dt.anchor}_config_modal"},
                )
            )
        if self.show_table:
            buttons.append(
                self._btn(
                    cls="mqc-violin-to-table",
                    label="<span class='glyphicon glyphicon-th-list'></span> Table",
                    attrs={"title": "Show as a table"},
                    data_attrs={"table-anchor": self.table_anchor, "violin-anchor": self.anchor, "toggle": "tooltip"},
                )
            )

        return buttons + super().buttons(flat=flat, module_anchor=module_anchor, section_anchor=section_anchor)

    def show(
        self,
        dataset_id: Union[int, str] = 0,
        flat: bool = False,
        table: Optional[bool] = None,
        violin: Optional[bool] = None,
        **kwargs,
    ):
        """
        Show the table or violin plot based on the input parameters.
        """
        if self.show_table_by_default and violin is not True or table is True:
            # `dataset_id` and `flat` are derived from the parent class and ignored, as for this plot
            # we only support one dataset, and the flat mode is not applicable.

            data: Dict[str, Dict[str, Union[int, float, str, None]]] = {}
            for idx, col_key, header in self.datasets[0].dt.get_headers_in_order():
                rid = header.rid
                for _, group_rows in self.datasets[0].dt.sections[idx].rows_by_sgroup.items():
                    for row in group_rows:
                        if col_key in row.raw_data:
                            val = row.raw_data[col_key]
                            data.setdefault(row.sample, {})[rid] = val

            import pandas as pd  # type: ignore

            df = pd.DataFrame(data)
            # Showing the first column header if it's not default
            if self.pconfig.col1_header != TableConfig.model_fields["col1_header"].default:
                df.index.name = self.pconfig.col1_header
            return df.T  # Jupyter knows how to display dataframes

        else:
            return super().show(dataset_id, flat, **kwargs)

    def save(
        self,
        filename: str,
        dataset_id: Union[int, str] = 0,
        flat: Optional[bool] = None,
        table: Optional[bool] = None,
        violin: Optional[bool] = None,
        **kwargs,
    ):
        """
        Save the plot to a file
        """
        if self.show_table_by_default and violin is not True or table is True:
            # Make Plotly go.Table object and save it
            data: Dict[str, Dict[str, Union[int, float, str, None]]] = {}
            for idx, metric, header in self.datasets[0].dt.get_headers_in_order():
                rid = header.rid
                for _, group_rows in self.datasets[0].dt.sections[idx].rows_by_sgroup.items():
                    for row in group_rows:
                        if metric in row.raw_data:
                            val = row.raw_data[metric]
                            data.setdefault(row.sample, {})[rid] = val

            values: List[List[Any]] = [list(data.keys())]
            for idx, metric, header in self.datasets[0].dt.get_headers_in_order():
                rid = header.rid
                values.append([data[s].get(rid, "") for s in data.keys()])

            keys = list(data.keys())
            if not keys:
                logger.error("No plot data found to save")
                return

            fig = go.Figure(
                data=[
                    go.Table(
                        header=dict(values=["Sample"] + list(data[list(data.keys())[0]].keys())),
                        cells=dict(values=values),
                    )
                ],
                layout=self.layout,
            )
            filename, flat = self._proc_save_args(filename, flat)
            if flat:
                fig.write_image(
                    filename,
                    scale=2,
                    width=fig.layout.width,
                    height=fig.layout.height,
                )
            else:
                fig.write_html(
                    filename,
                    include_plotlyjs="cdn",
                    full_html=False,
                )
        else:
            super().save(filename, **kwargs)

    def add_to_report(self, module_anchor: Anchor, section_anchor: Anchor, plots_dir_name: Optional[str] = None) -> str:
        warning = ""
        if self.show_table_by_default and not self.show_table:
            warning = (
                f'<p class="text-muted" id="table-violin-info-{self.anchor}">'
                + '<span class="glyphicon glyphicon-exclamation-sign" '
                + 'title="An interactive table is not available because of the large number of samples. '
                + "A violin plot is generated instead, showing density of values for each metric, as "
                + 'well as hoverable points for outlier samples in each metric."'
                + f' data-toggle="tooltip"></span> Showing {self.n_samples} samples.</p>'
            )
        elif not self.show_table:
            warning = (
                f'<p class="text-muted" id="table-violin-info-{self.anchor}">'
                + '<span class="glyphicon glyphicon-exclamation-sign" '
                + 'title="An interactive table is not available because of the large number of samples. '
                + "The violin plot displays hoverable points only for outlier samples in each metric, "
                + 'and the hiding/highlighting functionality through the toolbox only works for outliers"'
                + f' data-toggle="tooltip"></span> Showing {self.n_samples} samples.</p>'
            )

        assert self.datasets[0].dt is not None
        # Render both, add a switch between table and violin
        table_html, configuration_modal = make_table(
            self.datasets[0].dt,
            violin_anchor=self.anchor,
            module_anchor=module_anchor,
            section_anchor=section_anchor,
        )

        if not self.show_table:
            # Show violin alone.
            html = warning + super().add_to_report(
                plots_dir_name=plots_dir_name,
                module_anchor=module_anchor,
                section_anchor=section_anchor,
            )
        else:
            violin_html = super().add_to_report(
                plots_dir_name=plots_dir_name,
                module_anchor=module_anchor,
                section_anchor=section_anchor,
            )

            violin_visibility = "style='display: none;'" if self.show_table_by_default else ""
            html = f"<div id='mqc_violintable_wrapper_{self.anchor}' {violin_visibility}>{warning}{violin_html}</div>"

            table_visibility = "style='display: none;'" if not self.show_table_by_default else ""
            html += (
                f"<div id='mqc_violintable_wrapper_{self.datasets[0].dt.anchor}' {table_visibility}>{table_html}</div>"
            )

        html += configuration_modal

        return html

    def _plot_ai_header(self) -> str:
        if self.show_table_by_default:
            return "Plot type: table\n"
        return "Plot type: violin plot\n"


def find_outliers(
    values: List[Union[int, float]],
    top_n: Optional[int] = None,
    z_cutoff: float = 2.0,
    minval: Optional[Union[float, int]] = None,
    maxval: Optional[Union[float, int]] = None,
    metric: Optional[str] = None,
) -> np.ndarray:
    """
    If `n` is defined, find `n` most outlying points in a list.
    Otherwise, find outliers with a Z-score above `z_cutoff`.

    Return a list of booleans, indicating if the value with this index is an outlier.

    `minval` and/or `maxval` can be added to include them into the outlier detection array.
    This is a trick to avoid the following problem: for example, percentage values are
    clustered around 0, but differ in e.g. 3+rd decimal place. In this case, the outliers
    will be nearly no different from the other values. If we add "100%" as an artificial
    additional value, none of those near-zero clustered values won't be called outliers.
    """
    if len(values) == 0 or (top_n is not None and top_n <= 0):
        return np.zeros(len(values), dtype=bool)

    added_values: List[Union[int, float]] = []
    if minval is not None:
        added_values.append(minval)
    if maxval is not None:
        added_values.append(maxval)
    np_values = np.array(values + added_values)
    del values

    # Calculate the mean and standard deviation
    mean = np.mean(np_values)
    std_dev = np.std(np_values)
    if std_dev == 0:
        logger.debug(f"All {len(np_values)} points have the same values" + (f", metric: '{metric}'" if metric else ""))
        return np.zeros(len(np_values), dtype=bool)

    # Calculate Z-scores (measures of "outlyingness")
    z_scores = np.abs((np_values - mean) / std_dev)

    # Get indices of the top N outliers
    outlier_status = np.zeros(len(np_values), dtype=bool)
    if top_n:
        outlier_status[np.argsort(z_scores)[-top_n:]] = True
    else:
        indices = np.where(z_scores > z_cutoff)[0]
        while len(indices) <= len(added_values) and z_cutoff > 1.0:
            # No outliers found with this Z-score cutoff, trying a lower cutoff
            z_cutoff -= 0.2
            indices = np.where(z_scores > z_cutoff)[0]
        outlier_status[indices] = True

    if added_values:
        outlier_status = outlier_status[: -len(added_values)]  # type: ignore
    return outlier_status
