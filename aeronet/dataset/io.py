import os
import rasterio
from tqdm import tqdm

from .raster import Band
from .raster import BandCollection


class SequentialSampler:

    def __init__(self, band_collection, channels, sample_size, bound=0):
        """ Iterate over BandCollection sequentially with specified shape (+ bounds)
        Args:
            band_collection: BandCollection instance
            channels: list of str, required channels with required order
            sample_size: (height, width), size of `pure` sample in pixels (bounds not included)
            bound: int, bounds in pixels added to sample
        Return:
            Iterable object (yield SampleCollection instances)
        """

        self.band_collection = band_collection
        self.sample_size = sample_size
        self.bound = bound
        self.channels = channels
        self.blocks = self._compute_blocks()

    def __len__(self):
        return len(self.blocks)

    def __getitem__(self, i):
        block = self.blocks[i]
        sample = (self.band_collection
                  .ordered(*self.channels)
                  .sample(block['y'], block['x'], block['height'], block['width']))
        return sample, block

    def _compute_blocks(self):

        h, w = self.sample_size
        blocks = []
        height = h + 2 * self.bound
        width = w + 2 * self.bound

        for y in range(- self.bound, self.band_collection.height, h):
            for x in range(- self.bound, self.band_collection.width, w):

                rigth_x_bound = max(self.bound,
                                    x + width - self.band_collection.width)
                bottom_y_bound = max(self.bound,
                                    y + height - self.band_collection.height)

                blocks.append({'x': x,
                               'y': y,
                               'height': height,
                               'width': width,
                               'bounds':
                                   [[self.bound, bottom_y_bound], [self.bound, rigth_x_bound]],
                               })
        return blocks


class SampleWindowWriter:

    def __init__(self, fp, shape, transform, crs, nodata, dtype='uint8'):
        """ Create empty `Band` (rasterio open file) and write blocks sequentially

        Args:
            fp: file path of created Band
            shape: (height, width), size of band in pixels
            transform: rasterio Affine object
            crs: rasterio CRS or epsg core of coordinate system
            nodata: value of pixels without data
            dtype: str, one of rasterio data types

        Returns:
            when closed return `Band`

        Examples:
            ```python
            # create band
            bc = BandCollection(['/path/to/RED.tif', '/path/to/GRN.tif'])

            src = SequentialSampler(bc, channels, (1024, 1024), 512)
            dst = SampleWindowWriter('./test.tif', src.shape, **bc.profile)

            for sample, block in src:
                # read raster
                raster = sample.ordered('RED').numpy()

                # transform raster
                raster += 1

                # write raster
                dst.write(raster, **block)

            # close file when all data precessed
            created_band = dst.close()
            ```
        """
        self.fp = fp
        self.shape = shape
        self.transform = transform
        self.nodata = nodata
        self.crs = crs
        self.dtype = dtype
        self.dst = self.open()

    @property
    def height(self):
        return self.shape[0]

    @property
    def width(self):
        return self.shape[1]

    def open(self):
        return rasterio.open(self.fp, 'w', driver='GTiff', transform=self.transform, crs=self.crs,
                                 height=self.height, width=self.width, count=1,
                                 dtype=self.dtype, nodata=self.nodata)

    def close(self):
        self.dst.close()
        return Band(self.fp)

    def write(self, raster, x, y, width, height, bounds=None):
        """ Writes the specified raster into a window in dst
        The raster boundaries can be cut by 'bounds' pixels to prevent boundary effects on the algorithm output.
        If width and height are not equal to size of raster (after the bounds are cut), which is not typical,
        the raster is stretched to the window size (width and height)

        Args:
            raster: numpy array to be written into dst
            x: begin position of window
            y: begin position of window
            width: size of window
            height: size of window
            bounds: [[,][,]] - number of pixels to cut off from each side of the raster before writing

        Returns:

        """


        if bounds:
            raster = raster[bounds[0][0]:raster.shape[0]-bounds[0][1], bounds[1][0]:raster.shape[1]-bounds[1][1]]
            x += bounds[1][0]
            y += bounds[0][0]
            width = width - bounds[1][1] - bounds[1][0]
            height = height - bounds[0][1] - bounds[0][0]

        self.dst.write(raster, 1, window=((y, y+height), (x, x+width)))


class SampleCollectionWindowWriter:

    def __init__(self, directory, channels, shape, transform, crs, nodata, dtype='uint8'):
        """ Create empty `Band` (rasterio open file) and write blocks sequentially

        Args:
            direcory: directory path of created BandCollection
            channels: channel names of created BandCollection
            shape: (height, width), size of band in pixels
            transform: rasterio Affine object
            crs: rasterio CRS or epsg core of coordinate system
            nodata: value of pixels without data
            dtype: str, one of rasterio data types

        Returns:
            when closed return `BandCollection`

        Examples:
            ```python
            # create band
            bc = BandCollection(['/path/to/RED.tif', '/path/to/GRN.tif'])

            src = SequentialSampler(bc, channels, (1024, 1024), 512)
            dst = SampleCollectionWindowWriter('./test.tif', src.shape, **bc.profile)

            for sample, block in src:
                # read raster
                raster = sample.numpy()

                # transform raster
                raster += 1

                # write raster
                dst.write(raster, **block)

            # close file when all data precessed
            created_bc = dst.close()
            ```
        """
        if directory:
            os.makedirs(directory, exist_ok=True)

        self.fps = [os.path.join(directory, channel + '.tif') for channel in channels]
        self.channels = channels
        self.shape = shape
        self.transform = transform
        self.nodata = nodata
        self.crs = crs
        self.dtype = dtype
        self.writers = self.open()

    def open(self):
        writers = []
        for fp in self.fps:
            writers.append(
                SampleWindowWriter(fp, self.shape, self.transform,
                                   self.crs, self.nodata, self.dtype)
            )
        return writers

    def write(self, raster, x, y, height, width, bounds=None):
        for i in range(len(self.channels)):
            self.writers[i].write(raster[i], x, y, height, width, bounds=bounds)

    def close(self):
        bands = [w.close() for w in self.writers]
        return BandCollection(bands)


class Predictor:

    def __init__(self, input_channels, output_labels,
                 sample_size=(1024, 1024), bound=256, **kwargs):
        """
        Args:
            input_channels: list of str, names of bands/channels
            output_labels: list of str, names of output classes
            sample_size: (height, width), size of `pure` sample in pixels (bounds not included)
            bound: int, non-negative bounds in pixels added to sample

        Returns:
            processed BandCollection
        """

        self.input_channels = input_channels
        self.output_labels = output_labels
        self.sample_size = sample_size
        self.bound = bound
        self.kwargs = kwargs

    def predict(self, sample, **kwargs):
        """ Abstract function for prediction on a single data sample.
        It should be reimplemented in inherited classes for use.
        Usage example:

        class MyPredictor(Predictor):

            def __init__(self, model, *args, **kwargs):
                super.__init__(*args, **kwargs)
                self.model = model

            def predict(self, sample, threshold):
                x = sample.numpy().transpose(1,2,0)
                x = np.expand_dims(x, 0)
                y = self.model.predict(x)
                return y.squeeze(0).transpose(2,0,1)

        Args:
            sample: BandSample from sampler
            **kwargs: any keyword arguments to be specified in subclasses

        Returns:
            numpy array that can be written to the dst

        """
        raise NotImplemented('You should implement the predict function in your inherited class')

    def process(self, bc, output_directory, **kwargs):
        """

        Args:
            bc: BandCollection to be processed
            output_directory: where the result will be written
            **kwargs: any keyword args to be passed to 'predict'

        Returns:
            resulting BandCollection
        """

        src = SequentialSampler(bc, self.input_channels, self.sample_size, self.bound)
        dst = SampleCollectionWindowWriter(output_directory, self.output_labels,
                                           bc.shape[1:], **bc.profile, **self.kwargs)

        for sample, block in tqdm(src):
            raster = self.predict(sample, **kwargs)
            dst.write(raster, **block)

        return dst.close()

