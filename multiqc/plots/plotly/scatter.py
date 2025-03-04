import copy
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple, Union, cast

import numpy as np
from plotly import graph_objects as go  # type: ignore

from multiqc import report
from multiqc.plots.plotly.plot import BaseDataset, PConfig, Plot, PlotType
from multiqc.types import SampleName

logger = logging.getLogger(__name__)


class ScatterConfig(PConfig):
    categories: Optional[List[str]] = None
    extra_series: Union[Dict[str, Any], List[Dict[str, Any]], List[List[Dict[str, Any]]], None] = None
    marker_size: Optional[int] = None
    marker_line_width: Optional[int] = None
    color: Optional[str] = None
    opacity: Optional[float] = None

    def __init__(self, path_in_cfg: Optional[Tuple[str, ...]] = None, **data):
        super().__init__(path_in_cfg=path_in_cfg or ("scatterplot",), **data)


# {'color': 'rgb(211,211,211,0.05)', 'name': 'background: EUR', 'x': -0.294, 'y': -1.527}
ValueT = Union[str, float, int]
PointT = Dict[str, ValueT]


def plot(points_lists: List[List[PointT]], sample_names: List[SampleName], pconfig: ScatterConfig) -> "ScatterPlot":
    """
    Build and add the plot data to the report, return an HTML wrapper.
    :param points_lists: each dataset is a 2D dict, first keys as sample names, then x:y data pairs
    :param pconfig: dict with config key:value pairs. See CONTRIBUTING.md
    :return: HTML with JS, ready to be inserted into the page
    """
    return ScatterPlot.create(pconfig, points_lists, sample_names)


class Dataset(BaseDataset):
    points: List[PointT]

    @staticmethod
    def create(
        dataset: BaseDataset,
        points: List[Dict[str, Any]],
        pconfig: ScatterConfig,
    ) -> "Dataset":
        dataset = Dataset(
            **dataset.__dict__,
            points=points,
        )

        dataset.trace_params.update(
            textfont=dict(size=8),
            marker=dict(
                size=10,
                line=dict(width=1),
                opacity=1,
                color="rgba(124, 181, 236, .5)",
            ),
        )

        # if categories is provided, set them as x-axis ticks
        if pconfig.categories:
            dataset.layout["xaxis"]["tickmode"] = "array"
            dataset.layout["xaxis"]["tickvals"] = list(range(len(pconfig.categories)))
            dataset.layout["xaxis"]["ticktext"] = pconfig.categories

        return dataset

    def create_figure(
        self,
        layout: go.Layout,
        is_log: bool = False,
        is_pct: bool = False,
        **kwargs,
    ) -> go.Figure:
        """
        Create a Plotly figure for a dataset
        """
        fig = go.Figure(layout=layout)

        MAX_ANNOTATIONS = 10  # Maximum number of dots to be annotated directly on the plot
        n_annotated = len([el for el in self.points if "annotation" in el])
        if n_annotated < MAX_ANNOTATIONS:
            points = [(i, x) for i, x in enumerate(self.points) if x.get("annotate", True) is not False]
            x_values = np.array([x["x"] for (i, x) in points])
            y_values = np.array([x["y"] for (i, x) in points])

            x_are_numeric = np.issubdtype(x_values.dtype, np.number)
            y_are_numeric = np.issubdtype(y_values.dtype, np.number)

            if x_are_numeric and y_are_numeric:
                # Finding and marking outliers to only label them
                # 1. Calculate Z-scores
                x_std = np.std(x_values)
                y_std = np.std(y_values)
                if x_std == 0 and y_std == 0:
                    logger.warning(f"Scatter plot {self.plot_id}: all {len(points)} points have the same coordinates")
                    if len(points) == 1:  # Only single point - annotate it!
                        for _, point in points:
                            point["annotation"] = point["name"]
                else:
                    x_z_scores = np.abs((x_values - np.mean(x_values)) / x_std) if x_std else np.zeros_like(x_values)
                    y_z_scores = np.abs((y_values - np.mean(y_values)) / y_std) if y_std else np.zeros_like(y_values)
                    # 2. Find a reasonable threshold so there are not too many outliers
                    threshold = 1.0
                    while threshold <= 6.0:
                        n_outliers = np.count_nonzero((x_z_scores > threshold) | (y_z_scores > threshold))
                        # logger.debug(f"Scatter plot outlier threshold: {threshold:.2f}, outliers: {n_outliers}")
                        if n_annotated + n_outliers <= MAX_ANNOTATIONS:
                            break
                        # If there are too many outliers, we increase the threshold until we have less than 10
                        threshold += 0.2
                    # 3. Annotate outliers that pass the threshold
                    for (_, point), x_z_score, y_z_score in zip(points, x_z_scores, y_z_scores):
                        # Check if point is an outlier or if total points are less than 10
                        if x_z_score > threshold or y_z_score > threshold:
                            point["annotation"] = point["name"]
                n_annotated = len([point for point in self.points if "annotation" in point])

        # If there are few unique colors, we can additionally put a unique list into a legend
        # (even though some color might belong to many distinct names - we will just crop the list)
        names_by_legend_key: Dict[Tuple[Any, Any, Any, Any], Set[str]] = defaultdict(set)
        for el in self.points:
            legend_key = (el.get("color"), el.get("marker_size"), el.get("marker_line_width"), el.get("group"))
            name = el["name"]
            assert isinstance(name, str)
            names_by_legend_key[legend_key].add(name)
        layout.showlegend = True

        in_legend: Set[Tuple[Any, Any, Any, Any]] = set()
        for el in self.points:
            x = el["x"]
            name = el["name"]
            group = el.get("group")
            color = el.get("color")
            annotation = el.get("annotation")

            show_in_legend = False
            if layout.showlegend and not el.get("hide_in_legend"):
                key = (color, el.get("marker_size"), el.get("marker_line_width"), group)
                if key not in in_legend:
                    in_legend.add(key)
                    names = sorted(names_by_legend_key[key])
                    label = ", ".join(names)
                    if group:
                        label = f"{group}: {label}"
                    MAX_WIDTH = 60
                    if len(label) > MAX_WIDTH:  # Crop long labels
                        label = label[:MAX_WIDTH] + "..."
                    show_in_legend = True
                    name = label

            params = copy.deepcopy(self.trace_params)
            marker = params.pop("marker")
            if color:
                marker["color"] = color

            if "marker_line_width" in el:
                marker["line"]["width"] = el["marker_line_width"]
            if "marker_size" in el:
                marker["size"] = el["marker_size"]
            if "opacity" in el:
                marker["opacity"] = el["opacity"]

            if annotation:
                params["mode"] = "markers+text"
            if n_annotated > 0:  # Reduce opacity of the borders that clutter the annotations:
                marker["line"]["color"] = "rgba(0, 0, 0, .2)"

            fig.add_trace(
                go.Scatter(
                    x=[x],
                    y=[el["y"]],
                    name=name,
                    text=[annotation or name],
                    showlegend=show_in_legend,
                    marker=marker,
                    **params,
                )
            )
        fig.layout.height += len(in_legend) * 5  # extra space for legend
        return fig

    def get_x_range(self) -> Tuple[Optional[Any], Optional[Any]]:
        if not self.points:
            return None, None
        xmax, xmin = None, None
        for point in self.points:
            x = point["x"]
            if xmax is not None:
                xmax = max(xmax, x)
            else:
                xmax = x
            if xmin is not None:
                xmin = min(xmin, x)
            else:
                xmin = x
        return xmin, xmax

    def get_y_range(self) -> Tuple[Optional[Any], Optional[Any]]:
        if not self.points:
            return None, None
        ymax, ymin = None, None
        for point in self.points:
            y = point["y"]
            if ymax is not None:
                ymax = max(ymax, y)
            else:
                ymax = y
            if ymin is not None:
                ymin = min(ymin, y)
            else:
                ymin = y
        return ymin, ymax

    def save_data_file(self) -> None:
        data = [
            {
                "Name": point["name"],
                "X": point["x"],
                "Y": point["y"],
            }
            for point in self.points
        ]
        report.write_data_file(data, self.uid)

    def format_dataset_for_ai_prompt(self, pconfig: PConfig, keep_hidden: bool = True) -> str:
        xsuffix = self.layout.get("xaxis", {}).get("ticksuffix", "")
        ysuffix = self.layout.get("yaxis", {}).get("ticksuffix", "")

        prompt = ""
        prompt += "|Sample|X|Y|\n"
        prompt += "|---|---|---|\n"

        for point in self.points:
            pseudonym = report.anonymize_sample_name(cast(str, point["name"]))
            prompt += f"|{pseudonym}|{point['x']}{xsuffix}|{point['y']}{ysuffix}|\n"

        return prompt


class ScatterPlot(Plot[Dataset, ScatterConfig]):
    datasets: List[Dataset]
    sample_names: List[SampleName]

    def samples_names(self) -> List[SampleName]:
        return self.sample_names

    @staticmethod
    def create(
        pconfig: ScatterConfig, points_lists: List[List[PointT]], sample_names: List[SampleName]
    ) -> "ScatterPlot":
        model: Plot[Dataset, ScatterConfig] = Plot.initialize(
            plot_type=PlotType.SCATTER,
            pconfig=pconfig,
            n_samples_per_dataset=[len(x) for x in points_lists],
            default_tt_label="<br><b>X</b>: %{x}<br><b>Y</b>: %{y}",
        )

        model.datasets = [Dataset.create(d, points, pconfig) for d, points in zip(model.datasets, points_lists)]

        model._set_x_bands_and_range(pconfig)
        model._set_y_bands_and_range(pconfig)

        # Make a tooltip always show on hover over nearest point on plot
        model.layout.hoverdistance = -1

        return ScatterPlot(**model.__dict__, sample_names=sample_names)

    def _plot_ai_header(self) -> str:
        result = super()._plot_ai_header()
        if self.pconfig.xlab:
            result += f"X axis: {self.pconfig.xlab}\n"
        if self.pconfig.ylab:
            result += f"Y axis: {self.pconfig.ylab}\n"
        if self.pconfig.categories:
            result += f"X categories: {', '.join(self.pconfig.categories)}\n"
        return result
