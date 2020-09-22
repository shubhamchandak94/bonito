"""
Bonito Basecaller
"""

import sys
import time
from datetime import timedelta
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter

from bonito.io import write_sam_header, write_summary_header, summary_file
from bonito.io import ProcessPool, ProcessIterator, CTCWriter, DecoderWriter
from bonito.util import load_model, chunk, stitch, half_supported, get_reads, column_to_set

import torch
import numpy as np
from mappy import Aligner

def main(args):
    if args.save_ctc and not args.reference:
        sys.stderr.write("> a reference is needed to output ctc training data\n")
        exit(1)

    if args.save_ctc:
        args.overlap = 900
        args.chunksize = 3600

    sys.stderr.write("> loading model\n")

    model = load_model(
        args.model_directory, args.device, weights=int(args.weights),
        half=args.half, chunksize=args.chunksize, use_rt=args.cudart,
    )

    if args.reference:
        sys.stderr.write("> loading reference\n")
        aligner = Aligner(args.reference, preset='ont-map')
        if not aligner:
            sys.stderr.write("> failed to load/build index\n")
            sys.exit(1)
        write_sam_header(aligner)
    else:
        aligner = None

#    with open(summary_file(), 'w') as summary:
#        write_summary_header(summary, alignment=aligner)

    samples = 0
    num_reads = 0
    max_read_size = 4e6
    read_ids = column_to_set(args.read_ids)
    dtype = np.float16 if args.half else np.float32
    reader = ProcessIterator(
        get_reads(args.reads_directory, read_ids=read_ids, skip=args.skip), progress=True
    )
    writer = ProcessPool(
        DecoderWriter, model=model, aligner=aligner, beamsize=args.beamsize, fastq=args.fastq
    )
    ctc_writer = CTCWriter(
        model, aligner, min_coverage=args.ctc_min_coverage, min_accuracy=args.ctc_min_accuracy
    )

    t0 = time.perf_counter()
    sys.stderr.write("> calling\n")

    with writer, ctc_writer, reader, torch.no_grad():

        while True:

            read = reader.queue.get()
            if read is None:
                break

            if len(read.signal) > max_read_size:
                sys.stderr.write("> skipping long read %s (%s samples)\n" % (read.read_id, len(read.signal)))
                continue

            num_reads += 1
            samples += len(read.signal)

            raw_data = torch.tensor(read.signal.astype(dtype))
            print('bonito: raw_data.shape: ', raw_data.shape)
            chunks = chunk(raw_data, args.chunksize, args.overlap)
            
            posteriors_ = model(chunks.to(args.device)).cpu().numpy()
            posteriors = stitch(posteriors_, args.overlap // model.stride // 2)
            if args.write_basecall:
                writer.queue.put((read, posteriors[:raw_data.shape[0]]))
            if args.save_ctc and len(raw_data) > args.chunksize:
                ctc_writer.queue.put((chunks.numpy(), posteriors_))
            print('bonito: posteriors.shape', posteriors.shape)
            posteriors.tofile(args.post_file)

    duration = time.perf_counter() - t0

    sys.stderr.write("> completed reads: %s\n" % num_reads)
    sys.stderr.write("> duration: %s\n" % timedelta(seconds=np.round(duration)))
    sys.stderr.write("> samples per second %.1E\n" % (samples / duration))
    sys.stderr.write("> done\n")


def argparser():
    parser = ArgumentParser(
        formatter_class=ArgumentDefaultsHelpFormatter,
        add_help=False
    )
    parser.add_argument("model_directory")
    parser.add_argument("reads_directory")
    parser.add_argument("--reference")
    parser.add_argument("--read-ids")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--weights", default="0", type=str)
    parser.add_argument("--beamsize", default=5, type=int)
    parser.add_argument("--post_file",type=str,required=True)
    parser.add_argument("--write_basecall",default=False,action='store_true')
    parser.add_argument("--chunksize", default=0, type=int)
    parser.add_argument("--overlap", default=0, type=int)
    parser.add_argument("--half", action="store_true", default=half_supported())
    parser.add_argument("--skip", action="store_true", default=False)
    parser.add_argument("--fastq", action="store_true", default=False)
    parser.add_argument("--cudart", action="store_true", default=False)
    parser.add_argument("--save-ctc", action="store_true", default=False)
    parser.add_argument("--ctc-min-coverage", default=0.9, type=float)
    parser.add_argument("--ctc-min-accuracy", default=0.9, type=float)
    return parser
