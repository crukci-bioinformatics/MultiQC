"""Plotly bargraph functionality."""

import copy
import logging
import math
from collections import defaultdict
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple, Type, TypedDict, Union

import plotly.graph_objects as go  # type: ignore
import spectra  # type: ignore
from pydantic import BaseModel, model_validator

from multiqc import config, report
from multiqc.plots.plotly import determine_barplot_height
from multiqc.plots.plotly.plot import (
    BaseDataset,
    PConfig,
    Plot,
    PlotType,
    split_long_string,
)
from multiqc.types import SampleName

logger = logging.getLogger(__name__)


class BarPlotConfig(PConfig):
    stacking: Union[Literal["group", "overlay", "relative", "normal"], None] = "relative"
    hide_zero_cats: bool = True
    sort_samples: bool = True
    use_legend: Optional[bool] = None
    suffix: Optional[str] = None
    lab_format: Optional[str] = None

    def __init__(self, path_in_cfg: Optional[Tuple[str, ...]] = None, **data):
        if "suffix" in data:
            data["ysuffix"] = data["suffix"]
            del data["suffix"]
        if "lab_format" in data:
            data["ylab_format"] = data["lab_format"]
            del data["lab_format"]

        super().__init__(path_in_cfg=path_in_cfg or ("barplot",), **data)


SampleNameT = Union[SampleName, str]


class CatDataDict(TypedDict):
    name: str
    color: str
    data: List[float]
    data_pct: List[float]


def plot(
    cats_lists: Sequence[Sequence[CatDataDict]],
    samples_lists: Sequence[Sequence[SampleNameT]],
    pconfig: BarPlotConfig,
) -> "BarPlot":
    """
    Build and add the plot data to the report, return an HTML wrapper.
    :param cats_lists: each dataset is a list of dicts with the keys: {name, color, data},
        where `name` is the category name, `color` is the color of the bar,
        and `data` is a list of values for each sample. Each outer list will
        correspond a separate tab.
    :param samples_lists: list of lists of bar names (that is, sample names). Similarly,
        each outer list will correspond to a separate tab.
    :param pconfig: Plot configuration dictionary
    :return: HTML with JS, ready to be inserted into the page
    """
    return BarPlot.create(
        pconfig=pconfig,
        cats_lists=cats_lists,
        samples_lists=samples_lists,
    )


class Category(BaseModel):
    name: str
    color: str
    data: List[float]
    data_pct: List[float]


class Dataset(BaseDataset):
    cats: List[Category]
    samples: List[str]

    @staticmethod
    def create(
        dataset: BaseDataset,
        cats: Sequence[CatDataDict],
        samples: Sequence[str],
    ) -> "Dataset":
        # Need to reverse samples as the bar plot will show them reversed
        samples = list(reversed(samples))
        fixed_cats: List[Category] = []
        for input_cat in cats:
            if "name" not in input_cat:
                raise ValueError(f"Bar plot {dataset.plot_id}: missing 'name' key in category")

            # Split long category names
            name = "<br>".join(split_long_string(input_cat["name"]))

            # Reformat color to be ready to add alpha in Plotly-JS
            color = spectra.html(input_cat["color"])
            color_str = ",".join([f"{int(float(x) * 256)}" for x in color.rgb])

            # Reverse the data to match the reversed samples
            cat: Category = Category(
                name=name,
                color=color_str,
                data=list(reversed(input_cat["data"])),
                data_pct=list(reversed(input_cat["data_pct"])) if "data_pct" in input_cat else [],
            )

            # Check that the number of samples is the same for all categories
            assert len(samples) == len(cat.data)
            if cat.data_pct:
                assert len(samples) == len(cat.data_pct)

            fixed_cats.append(cat)

        dataset = Dataset(
            **dataset.model_dump(),
            cats=fixed_cats,
            samples=samples,
        )

        return dataset

    def create_figure(
        self,
        layout: go.Layout,
        is_log=False,
        is_pct=False,
        **kwargs,
    ) -> go.Figure:
        """
        Create a Plotly figure for a dataset
        """
        fig = go.Figure(layout=layout)

        for cat in self.cats:
            data = cat.data_pct if is_pct else cat.data

            params = copy.deepcopy(self.trace_params)
            assert cat.color is not None
            params["marker"]["color"] = f"rgb({cat.color})"
            fig.add_trace(
                go.Bar(
                    y=self.samples,
                    x=data,
                    name=cat.name,
                    meta=cat.name,
                    **params,
                ),
            )
        return fig

    def save_data_file(self) -> None:
        val_by_cat_by_sample: Dict[str, Dict[str, str]] = defaultdict(dict)
        for cat in self.cats:
            for d_idx, d_val in enumerate(cat.data):
                s_name = self.samples[d_idx]
                val_by_cat_by_sample[s_name][cat.name] = str(d_val)
        report.write_data_file(val_by_cat_by_sample, self.uid)

    def format_dataset_for_ai_prompt(self, pconfig: PConfig, keep_hidden: bool = True) -> str:
        """Format dataset as a markdown table"""
        prompt = ""
        prompt += "|Sample|" + "|".join(cat.name for cat in self.cats) + "|\n"
        prompt += "|---|" + "|".join("---" for _ in self.cats) + "|\n"

        suffix = ""
        if not pconfig.cpswitch_c_active:
            suffix += "%"
            if self.layout["xaxis"]["ticksuffix"] and self.layout["xaxis"]["ticksuffix"] != "%":
                suffix += " " + self.layout["xaxis"]["ticksuffix"]
        else:
            if self.layout["xaxis"]["ticksuffix"]:
                suffix += " " + self.layout["xaxis"]["ticksuffix"]

        for sidx, sample in enumerate(self.samples):
            presudonym = report.anonymize_sample_name(sample)
            prompt += (
                f"|{presudonym}|"
                + "|".join(
                    self.fmt_value_for_llm(
                        (cat.data if not pconfig.cpswitch or pconfig.cpswitch_c_active else cat.data_pct)[sidx]
                    )
                    + suffix
                    for cat in self.cats
                )
                + "|\n"
            )
        return prompt


class BarPlot(Plot[Dataset, BarPlotConfig]):
    datasets: List[Dataset]

    def samples_names(self) -> List[SampleName]:
        names: List[SampleName] = []
        for ds in self.datasets:
            names.extend(SampleName(sample) for sample in ds.samples)
        return names

    @staticmethod
    def create(
        pconfig: BarPlotConfig,
        cats_lists: Sequence[Sequence[CatDataDict]],
        samples_lists: Sequence[Sequence[SampleNameT]],
    ) -> "BarPlot":
        if len(cats_lists) != len(samples_lists):
            raise ValueError("Number of datasets and samples lists do not match")

        model: Plot[Dataset, BarPlotConfig] = Plot.initialize(
            plot_type=PlotType.BAR,
            pconfig=pconfig,
            n_samples_per_dataset=[len(x) for x in samples_lists],
            axis_controlled_by_switches=["xaxis"],
            default_tt_label="%{meta}: <b>%{x}</b>",
            defer_render_if_large=False,  # We hide samples on large bar plots, so no need to defer render
            flat_if_very_large=True,  # However, the data is still embedded into the HTML, and we don't want the report size to inflate
        )

        model.datasets = [
            Dataset.create(d, cats=cats, samples=samples)
            for d, cats, samples in zip(model.datasets, cats_lists, samples_lists)
        ]

        # Set the barmode
        barmode = pconfig.stacking  # stacking, but drawing negative values below zero
        if barmode is None:  # For legacy reasons, interpreting non-default None as "group"
            barmode = "group"  # side by side
        if barmode == "normal":  # Legacy
            barmode = "relative"

        max_n_cats = max([len(dataset.cats) for dataset in model.datasets])

        # Set height to also be proportional to the number of cats to fit a legend
        HEIGHT_PER_LEGEND_ITEM = 19
        legend_height = HEIGHT_PER_LEGEND_ITEM * max_n_cats

        max_n_samples = max(len(x) for x in samples_lists) if len(samples_lists) > 0 else 0
        height = determine_barplot_height(
            max_n_samples=max_n_samples,
            # Group mode puts each category in a separate bar, so need to multiply by the number of categories
            max_bars_in_group=max_n_cats if barmode == "group" else 1,
            legend_height=legend_height,
        )

        model.layout.update(
            height=height,
            barmode=barmode,
            bargroupgap=0,
            bargap=0.2,
            yaxis=dict(
                showgrid=False,
                categoryorder="trace",  # keep sample order
                automargin=True,  # to make sure there is enough space for ticks labels
                title=None,
                hoverformat=model.layout.xaxis.hoverformat,
                ticksuffix=model.layout.xaxis.ticksuffix,
                # Prevent JavaScript from automatically parsing categorical values as numbers:
                type="category",
            ),
            xaxis=dict(
                title=dict(text=model.layout.yaxis.title.text),
                hoverformat=model.layout.yaxis.hoverformat,
                ticksuffix=model.layout.yaxis.ticksuffix,
            ),
            # Re-initiate legend to reset to default legend location on the top right
            legend=go.layout.Legend(
                # We use legend groups with subplots to simulate standard legend interactivity
                # like we had a standard bar graph without subplots. We need to remove the space
                # between the legend groups to make it look like a single legend.
                tracegroupgap=0,
                # Plotly plots the grouped bar graph in a reversed order in respect to
                # the legend, so reversing the legend to match it:
                traceorder="normal" if barmode != "group" else "reversed",
            ),
            hovermode="y unified",
            hoverlabel=dict(
                bgcolor="rgba(255, 255, 255, 0.8)",
                font=dict(color="black"),
            ),
            showlegend=pconfig.use_legend if pconfig.use_legend is not None else True,
        )

        if getattr(config, "barplot_legend_on_bottom", False):
            model.layout.update(
                legend=go.layout.Legend(
                    orientation="h",
                    x=0.5,
                    xanchor="center",
                    y=-0.5,
                    yanchor="top",
                ),
            )

        for dataset in model.datasets:
            if barmode == "group":
                # max category
                xmax_cnt = max(max(cat.data[i] for cat in dataset.cats) for i in range(len(dataset.samples)))
                xmin_cnt = min(min(cat.data[i] for cat in dataset.cats) for i in range(len(dataset.samples)))
            else:
                # max sum of all categories across all samples
                xmax_cnt = max(
                    sum(cat.data[i] if cat.data[i] > 0 else 0 for cat in dataset.cats)
                    for i in range(len(dataset.samples))
                )
                xmin_cnt = min(
                    sum(cat.data[i] if cat.data[i] < 0 else 0 for cat in dataset.cats)
                    for i in range(len(dataset.samples))
                )

            minallowed = 0 if xmin_cnt > 0 else xmin_cnt  # allow bar to start below zero
            maxallowed = dataset.layout["yaxis"]["autorangeoptions"]["maxallowed"]
            if maxallowed is None:
                maxallowed = xmax_cnt

            dataset.layout.update(
                yaxis=dict(
                    title=None,
                    hoverformat=dataset.layout["xaxis"]["hoverformat"],
                    ticksuffix=dataset.layout["xaxis"]["ticksuffix"],
                ),
                xaxis=dict(
                    title=dict(text=dataset.layout["yaxis"]["title"]["text"]),
                    hoverformat=dataset.layout["yaxis"]["hoverformat"],
                    ticksuffix=dataset.layout["yaxis"]["ticksuffix"],
                    autorangeoptions=dict(
                        minallowed=minallowed,
                        maxallowed=maxallowed,
                    ),
                ),
                showlegend=len(dataset.cats) > 1 if pconfig.use_legend is None else pconfig.use_legend,
            )
            dataset.trace_params.update(
                orientation="h",
                marker=dict(line=dict(width=0)),
                textposition="inside",
                insidetextanchor="start",
            )
            if "hovertemplate" in dataset.trace_params:
                # %{text} doesn't work for unified hovermode:
                dataset.trace_params["hovertemplate"] = dataset.trace_params["hovertemplate"].replace("%{text}", "")

            if dataset.layout["xaxis"]["hoverformat"] is None:
                if all(all(isinstance(x, float) or math.isnan(x) for x in cat.data) for cat in dataset.cats):
                    dataset.layout["xaxis"]["hoverformat"] = ",.2f"
                elif all(all(isinstance(x, int) or math.isnan(x) for x in cat.data) for cat in dataset.cats):
                    dataset.layout["xaxis"]["hoverformat"] = ",.0f"

        # Expand data with zeroes if there are fewer values than samples
        for dataset in model.datasets:
            for cat in dataset.cats:
                if len(cat.data) < len(dataset.samples):
                    cat.data.extend([0] * (len(dataset.samples) - len(cat.data)))

        # Calculate and save percentages
        if model.add_pct_tab:
            for _, dataset in enumerate(model.datasets):
                # Count totals for each category
                sums: List[float] = [0 for _ in dataset.cats[0].data]
                for cat in dataset.cats:
                    for sample_idx, val in enumerate(cat.data):
                        if not math.isnan(val):
                            sums[sample_idx] += abs(val)

                # Now, calculate percentages for each category
                for cat in dataset.cats:
                    values = [x for x in cat.data]
                    for sample_idx, val in enumerate(values):
                        sum_for_sample = sums[sample_idx]
                        if sum_for_sample == 0:
                            values[sample_idx] = 0
                        else:
                            values[sample_idx] = float(val + 0.0) / float(sum_for_sample) * 100.0
                    cat.data_pct = values

                if barmode == "group":
                    # calculating the min percentage range as well because it will be negative for negative values
                    dataset.pct_range["xaxis"]["min"] = min(
                        min(cat.data_pct[i] for cat in dataset.cats) for i in range(len(dataset.samples))
                    )
                else:
                    dataset.pct_range["xaxis"]["min"] = min(
                        sum(cat.data_pct[i] if cat.data_pct[i] < 0 else 0 for cat in dataset.cats)
                        for i in range(len(dataset.samples))
                    )

        if model.add_log_tab:
            # Sorting from small to large so the log switch makes sense
            for dataset in model.datasets:
                dataset.cats.sort(key=lambda cat: sum(cat.data))
                # But reversing the legend so the largest bars are still on the top
                model.layout.legend.traceorder = "reversed"

        return BarPlot(**model.__dict__)

    def _plot_ai_header(self) -> str:
        result = super()._plot_ai_header()
        if self.pconfig.ylab:
            result += f"Values: {self.pconfig.ylab}\n"
        return result
