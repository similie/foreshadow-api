
import numpy as np
import numba
from numba import prange
import logging
# numba.set_num_threads(os.cpu_count())
logger = logging.getLogger(__name__)

@numba.njit
def evaluate_chunk(transform, simplices, vertex_values, query_pts, simplex_indices, start, end, ndim):
    """
    Evaluate the interpolator for a chunk of query points.
    """
    out = np.empty(end - start, dtype=np.float64)
    for i in range(start, end):
        s = simplex_indices[i]
        if s < 0:
            out[i - start] = np.nan
        else:
            # transform[s] is a (ndim+1, ndim) array: the last row is the offset.
            offset = transform[s, ndim, :]
            delta = query_pts[i] - offset
            b = np.empty(ndim, dtype=np.float64)
            sum_b = 0.0
            for j in range(ndim):
                temp = 0.0
                for k in range(ndim):
                    temp += transform[s, j, k] * delta[k]
                b[j] = temp
                sum_b += temp
            bary = np.empty(ndim + 1, dtype=np.float64)
            for j in range(ndim):
                bary[j] = b[j]
            bary[ndim] = 1.0 - sum_b
            verts = simplices[s]
            value = 0.0
            for j in range(ndim + 1):
                value += bary[j] * vertex_values[verts[j]]
            out[i - start] = value
    return out

# @numba.njit(parallel=True)
@numba.njit
def fast_interpolate(transform, simplices, vertex_values, query_pts, simplex_indices):
    """
    Evaluate the interpolator for all query points in parallel.

    Parameters:
      transform: Delaunay transform array (shape: [nsimplex, ndim+1, ndim]).
      simplices: Delaunay simplices (shape: [nsimplex, ndim+1]).
      vertex_values: 1D array of data values corresponding to each vertex.
      query_pts: Array of query points (shape: [n_points, ndim]).
      simplex_indices: 1D array of simplex indices (length n_points) as returned by tri.find_simplex().

    Returns:
      A 1D array of interpolated values (length n_points).
    """
    n_points = query_pts.shape[0]
    out = np.empty(n_points, dtype=np.float64)
    chunk_size = 5000  # adjust as needed
    n_chunks = (n_points + chunk_size - 1) // chunk_size
    ndim = query_pts.shape[1]
    for i in prange(n_chunks):
        start = i * chunk_size
        end = min(start + chunk_size, n_points)
        out[start:end] = evaluate_chunk(transform, simplices, vertex_values, query_pts, simplex_indices, start, end, ndim)
    return out
