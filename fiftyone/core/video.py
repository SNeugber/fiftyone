"""
Video frame views.

| Copyright 2017-2021, Voxel51, Inc.
| `voxel51.com <https://voxel51.com/>`_
|
"""
from copy import deepcopy
import logging
import os

from pymongo.errors import BulkWriteError

import eta.core.utils as etau

import fiftyone as fo
import fiftyone.core.dataset as fod
import fiftyone.core.fields as fof
import fiftyone.core.media as fom
import fiftyone.core.sample as fos
import fiftyone.core.odm as foo
import fiftyone.core.odm.sample as foos
import fiftyone.core.utils as fou
import fiftyone.core.validation as fova
import fiftyone.core.view as fov

fouv = fou.lazy_import("fiftyone.utils.video")


logger = logging.getLogger(__name__)


class FrameView(fos.SampleView):
    """A frame in a :class:`FramesView`.

    :class:`FrameView` instances should not be created manually; they are
    generated by iterating over :class:`FramesView` instances.

    Args:
        doc: a :class:`fiftyone.core.odm.DatasetSampleDocument`
        view: the :class:`FramesView` that the frame belongs to
        selected_fields (None): a set of field names that this view is
            restricted to
        excluded_fields (None): a set of field names that are excluded from
            this view
        filtered_fields (None): a set of field names of list fields that are
            filtered in this view
    """

    @property
    def _sample_id(self):
        return self._doc.sample_id

    def save(self):
        super().save()
        self._view._sync_source_sample(self)


class FramesView(fov.DatasetView):
    """A :class:`fiftyone.core.view.DatasetView` of frames from a video
    :class:`fiftyone.core.dataset.Dataset`.

    Frames views contain an ordered collection of frames, each of which
    corresponds to a single frame of a video from the source collection.

    Frames retrieved from frames views are returned as :class:`FrameView`
    objects.

    Args:
        source_collection: the
            :class:`fiftyone.core.collections.SampleCollection` from which this
            view was created
        frames_stage: the :class:`fiftyone.core.stages.ToFrames` stage that
            defines how the frames were created
        frames_dataset: the :class:`fiftyone.core.dataset.Dataset` that serves
            the frames in this view
    """

    _SAMPLE_CLS = FrameView

    def __init__(
        self, source_collection, frames_stage, frames_dataset, _stages=None
    ):
        if _stages is None:
            _stages = []

        self._source_collection = source_collection
        self._frames_stage = frames_stage
        self._frames_dataset = frames_dataset
        self.__stages = _stages

    def __copy__(self):
        return self.__class__(
            self._source_collection,
            deepcopy(self._frames_stage),
            self._frames_dataset,
            _stages=deepcopy(self.__stages),
        )

    @property
    def _base_view(self):
        return self.__class__(
            self._source_collection, self._frames_stage, self._frames_dataset,
        )

    @property
    def _dataset(self):
        return self._frames_dataset

    @property
    def _root_dataset(self):
        return self._source_collection._root_dataset

    @property
    def _stages(self):
        return self.__stages

    @property
    def _all_stages(self):
        return (
            self._source_collection.view()._all_stages
            + [self._frames_stage]
            + self.__stages
        )

    @property
    def name(self):
        return self.dataset_name + "-frames"

    @classmethod
    def _get_default_sample_fields(
        cls, include_private=False, use_db_fields=False
    ):
        fields = super()._get_default_sample_fields(
            include_private=include_private, use_db_fields=use_db_fields
        )

        if use_db_fields:
            return fields + ("_sample_id", "frame_number")

        return fields + ("sample_id", "frame_number")

    def set_values(self, field_name, *args, **kwargs):
        # The `set_values()` operation could change the contents of this view,
        # so we first record the sample IDs that need to be synced
        if self._stages:
            ids = self.values("_id")
        else:
            ids = None

        super().set_values(field_name, *args, **kwargs)

        field = field_name.split(".", 1)[0]
        self._sync_source(fields=[field], ids=ids)

    def save(self, fields=None):
        if etau.is_str(fields):
            fields = [fields]

        super().save(fields=fields)

        self._sync_source(fields=fields)

    def reload(self):
        self._root_dataset.reload()

        #
        # Regenerate the frames dataset
        #
        # This assumes that calling `load_view()` when the current patches
        # dataset has been deleted will cause a new one to be generated
        #

        self._frames_dataset.delete()
        _view = self._frames_stage.load_view(self._source_collection)
        self._frames_dataset = _view._frames_dataset

    def _sync_source_sample(self, sample):
        self._sync_source_schema(delete=False)

        default_fields = set(
            self._get_default_sample_fields(
                include_private=True, use_db_fields=True
            )
        )

        updates = {
            k: v
            for k, v in sample.to_mongo_dict().items()
            if k not in default_fields
        }

        if not updates:
            return

        match = {
            "_sample_id": sample._sample_id,
            "frame_number": sample.frame_number,
        }

        self._source_collection._dataset._frame_collection.update_one(
            match, {"$set": updates}
        )

    def _sync_source(self, fields=None, ids=None):
        default_fields = set(
            self._get_default_sample_fields(
                include_private=True, use_db_fields=True
            )
        )

        if fields is not None:
            fields = [f for f in fields if f not in default_fields]
            if not fields:
                return

        self._sync_source_schema(fields=fields, delete=True)

        dst_coll = self._source_collection._dataset._frame_collection_name

        pipeline = []

        if fields is None and ids is None:
            default_fields.discard("_id")
            default_fields.discard("_sample_id")
            default_fields.discard("frame_number")

            pipeline.extend(
                [{"$unset": list(default_fields)}, {"$out": dst_coll}]
            )
        else:
            if ids is not None:
                pipeline.append({"$match": {"_id": {"$in": ids}}})

            if fields is None:
                default_fields.discard("_sample_id")
                default_fields.discard("frame_number")

                pipeline.append({"$unset": list(default_fields)})
            else:
                project = {f: True for f in fields}
                project["_id"] = True
                project["_sample_id"] = True
                project["frame_number"] = True
                pipeline.append({"$project": project})

            pipeline.append(
                {
                    "$merge": {
                        "into": dst_coll,
                        "on": ["_sample_id", "frame_number"],
                        "whenMatched": "merge",
                        "whenNotMatched": "discard",
                    }
                }
            )

        self._frames_dataset._aggregate(pipeline=pipeline)

    def _sync_source_schema(self, fields=None, delete=False):
        schema = self.get_field_schema()
        src_schema = self._source_collection.get_frame_field_schema()

        add_fields = []
        delete_fields = []

        if fields is not None:
            # We're syncing specific fields; if they are not present in source
            # collection, add them

            for field_name in fields:
                if field_name not in src_schema:
                    add_fields.append(field_name)
        else:
            # We're syncing all fields; add any missing fields to source
            # collection and, if requested, delete any source fields that
            # aren't in this view

            default_fields = set(
                self._get_default_sample_fields(include_private=True)
            )

            for field_name in schema.keys():
                if (
                    field_name not in src_schema
                    and field_name not in default_fields
                ):
                    add_fields.append(field_name)

            if delete:
                for field_name in src_schema.keys():
                    if field_name not in schema:
                        delete_fields.append(field_name)

        for field_name in add_fields:
            field_kwargs = foo.get_field_kwargs(schema[field_name])
            self._source_collection._dataset.add_frame_field(
                field_name, **field_kwargs
            )

        if delete:
            for field_name in delete_fields:
                self._source_collection._dataset.delete_frame_field(field_name)


def make_frames_dataset(
    sample_collection,
    sample_frames=True,
    frames_patt=None,
    fps=None,
    max_fps=None,
    size=None,
    min_size=None,
    max_size=None,
    force_sample=False,
    sparse=False,
    name=None,
    verbose=False,
):
    """Creates a dataset that contains one sample per video frame in the
    collection.

    By default, samples will be generated for every video frame at full
    resolution, but this method provides a variety of parameters that can be
    used to customize the sampling behavior.

    The returned dataset will contain all frame-level fields and the ``tags``
    of each video as sample-level fields, as well as a ``sample_id`` field that
    records the IDs of the parent sample for each frame.

    When ``sample_frames`` is True (the default), this method samples each
    video in the collection into a directory of per-frame images with the same
    basename as the input video with frame numbers/format specified by
    ``frames_patt``. If this method is run multiple times, existing frames will
    not be resampled unless ``force_sample`` is set to True.

    For example, if ``frames_patt = "%%06d.jpg"``, then videos with the
    following paths::

        /path/to/video1.mp4
        /path/to/video2.mp4
        ...

    would be sampled as follows::

        /path/to/video1/
            000001.jpg
            000002.jpg
            ...
        /path/to/video2/
            000001.jpg
            000002.jpg
            ...

    .. note::

        The returned dataset is independent from the source collection;
        modifying it will not affect the source collection.

    Args:
        sample_collection: a
            :class:`fiftyone.core.collections.SampleCollection`
        sample_frames (True): whether to sample the video frames (True) or
            set the ``filepath`` of each sample to the source video (False).
            Note that datasets generated with this parameter set to False
            cannot currently be viewed in the App
        frames_patt (None): a pattern specifying the filename/format to use to
            store the sampled frames, e.g., ``"%%06d.jpg"``. The default value
            is ``fiftyone.config.default_sequence_idx + fiftyone.config.default_image_ext``
        fps (None): an optional frame rate at which to sample each video's
            frames
        max_fps (None): an optional maximum frame rate at which to sample.
            Videos with frame rate exceeding this value are downsampled
        size (None): an optional ``(width, height)`` for each frame. One
            dimension can be -1, in which case the aspect ratio is preserved
        min_size (None): an optional minimum ``(width, height)`` for each
            frame. A dimension can be -1 if no constraint should be applied.
            The frames are resized (aspect-preserving) if necessary to meet
            this constraint
        max_size (None): an optional maximum ``(width, height)`` for each
            frame. A dimension can be -1 if no constraint should be applied.
            The frames are resized (aspect-preserving) if necessary to meet
            this constraint
        sparse (False): whether to only generate samples for non-empty frames,
            i.e., frame numbers for which :class:`fiftyone.core.frame.Frame`
            instances have explicitly been created
        force_sample (False): whether to resample videos whose sampled frames
            already exist
        name (None): a name for the returned dataset
        verbose (False): whether to log information about the frames that will
            be sampled, if any

    Returns:
        a :class:`fiftyone.core.dataset.Dataset`
    """
    fova.validate_video_collection(sample_collection)

    if sample_frames and frames_patt is None:
        frames_patt = (
            fo.config.default_sequence_idx + fo.config.default_image_ext
        )

    # We'll need frame counts
    sample_collection.compute_metadata()

    #
    # Create dataset with proper schema
    #

    dataset = fod.Dataset(name, _frames=True)
    dataset.media_type = fom.IMAGE
    dataset.add_sample_field(
        "sample_id", fof.ObjectIdField, db_field="_sample_id"
    )

    frame_schema = sample_collection.get_frame_field_schema()
    dataset._sample_doc_cls.merge_field_schema(frame_schema)

    # This index will be used when populating the collection now as well as
    # later when syncing the source collection
    dataset._sample_collection.create_index(
        [("_sample_id", 1), ("frame_number", 1)], unique=True
    )

    # Populate frames dataset
    ids_to_sample, frames_to_sample = _populate_frames(
        dataset,
        sample_collection,
        frames_patt,
        force_sample,
        sample_frames,
        fps,
        max_fps,
        sparse,
        verbose,
    )

    # Sample video frames, if necessary
    if ids_to_sample:
        logger.info("Sampling video frames...")
        fouv.sample_videos(
            sample_collection.select(ids_to_sample),
            frames_patt=frames_patt,
            frames=frames_to_sample,
            size=size,
            min_size=min_size,
            max_size=max_size,
            original_frame_numbers=True,
            force_sample=True,
        )

    return dataset


def _populate_frames(
    dataset,
    src_collection,
    frames_patt,
    force_sample,
    sample_frames,
    fps,
    max_fps,
    sparse,
    verbose,
):
    if sample_frames and verbose:
        logger.info("Determining frames to sample...")

    #
    # Initialize frames dataset with proper frames
    #

    docs = []
    ids_to_sample = []
    frames_to_sample = []

    samples = src_collection.select_fields()._aggregate(attach_frames=True)
    for sample in samples:
        sample_id = sample["_id"]
        video_path = sample["filepath"]
        tags = sample.get("tags", [])
        metadata = sample.get("metadata", {})
        frame_rate = metadata.get("frame_rate", None)
        total_frame_count = metadata.get("total_frame_count", -1)
        frame_ids_map = {
            f["frame_number"]: f["_id"] for f in sample.get("frames", [])
        }

        if sample_frames:
            outdir = os.path.splitext(video_path)[0]
            images_patt = os.path.join(outdir, frames_patt)
        else:
            images_patt = None

        doc_frame_numbers, sample_frame_numbers = _parse_video_frames(
            video_path,
            images_patt,
            total_frame_count,
            frame_rate,
            frame_ids_map,
            sample_frames,
            force_sample,
            sparse,
            fps,
            max_fps,
            verbose,
        )

        # note: [] means no frames, None means all frames
        if sample_frame_numbers != []:
            ids_to_sample.append(str(sample_id))
            frames_to_sample.append(sample_frame_numbers)

        for frame_number in doc_frame_numbers:
            if sample_frames:
                _filepath = images_patt % frame_number
            else:
                _filepath = video_path

            doc = {
                "filepath": _filepath,
                "tags": tags,
                "metadata": None,
                "frame_number": frame_number,
                "_media_type": "image",
                "_rand": foos._generate_rand(_filepath),
                "_sample_id": sample_id,
            }

            _id = frame_ids_map.get(frame_number, None)
            if _id is not None:
                doc["_id"] = _id

            docs.append(doc)

    if not docs:
        return ids_to_sample, frames_to_sample

    try:
        dataset._sample_collection.insert_many(docs)
    except BulkWriteError as bwe:
        msg = bwe.details["writeErrors"][0]["errmsg"]
        raise ValueError(msg) from bwe

    #
    # Merge existing frame data
    #

    pipeline = src_collection._pipeline(frames_only=True)
    pipeline.extend(
        [
            {
                "$merge": {
                    "into": dataset._sample_collection_name,
                    "on": ["_sample_id", "frame_number"],
                    "whenMatched": "merge",
                    "whenNotMatched": "discard",
                }
            },
        ]
    )

    src_collection._dataset._aggregate(pipeline=pipeline, attach_frames=False)

    return ids_to_sample, frames_to_sample


def _parse_video_frames(
    video_path,
    images_patt,
    total_frame_count,
    frame_rate,
    frame_ids_map,
    sample_frames,
    force_sample,
    sparse,
    fps,
    max_fps,
    verbose,
):
    #
    # Determine target frames, taking subsampling into account
    #

    if fps is not None or max_fps is not None:
        target_frame_numbers = fouv.sample_frames_uniform(
            total_frame_count, frame_rate, fps=fps, max_fps=max_fps
        )
    else:
        target_frame_numbers = None  # all frames

    #
    # Determine frames for which to generate documents
    #

    if target_frame_numbers is None:
        if sparse and total_frame_count < 0:
            doc_frame_numbers = sorted(frame_ids_map.keys())
        else:
            doc_frame_numbers = list(range(1, total_frame_count + 1))
    else:
        doc_frame_numbers = target_frame_numbers

    if sparse:
        doc_frame_numbers = [
            fn for fn in doc_frame_numbers if fn in frame_ids_map
        ]

    if not sample_frames:
        return doc_frame_numbers, []

    #
    # Determine frames that need to be sampled
    #

    if force_sample:
        if not sparse and target_frame_numbers is None:
            sample_frame_numbers = None  # all frames
        else:
            sample_frame_numbers = doc_frame_numbers
    else:
        # @todo is this too expensive?
        sample_frame_numbers = [
            fn
            for fn in doc_frame_numbers
            if not os.path.isfile(images_patt % fn)
        ]

        if not sparse and len(sample_frame_numbers) == len(doc_frame_numbers):
            sample_frame_numbers = None  # all frames

    if verbose:
        if sample_frame_numbers is None:
            logger.info(
                "Must sample all %d frames of '%s'",
                total_frame_count,
                video_path,
            )
        elif sample_frame_numbers != []:
            logger.info(
                "Must sample %d/%d frames of '%s'",
                len(sample_frame_numbers),
                total_frame_count,
                video_path,
            )
        else:
            logger.info("Required frames already present for '%s'", video_path)

    return doc_frame_numbers, sample_frame_numbers
